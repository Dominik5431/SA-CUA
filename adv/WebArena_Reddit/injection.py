import os
import json
import re
import shlex
import subprocess
import tempfile

import paramiko

sql_functions = """
DROP FUNCTION IF EXISTS search_all_tables(text, text);

CREATE OR REPLACE FUNCTION search_all_tables(
    search_text text,
    target_table text DEFAULT NULL
) RETURNS TABLE(
    table_name text,
    column_name text,
    found_text text,
    record_id text
) AS $func$
DECLARE
    current_table RECORD;
    current_column RECORD;
    query text;
    result RECORD;
BEGIN
    FOR current_table IN
        SELECT tables.table_schema, tables.table_name
        FROM information_schema.tables AS tables
        WHERE tables.table_schema = 'public'
        AND tables.table_type = 'BASE TABLE'
        AND (target_table IS NULL OR tables.table_name = target_table)
    LOOP
        FOR current_column IN SELECT columns.column_name
                    FROM information_schema.columns AS columns
                    WHERE columns.table_schema = current_table.table_schema
                    AND columns.table_name = current_table.table_name
                    AND (columns.data_type LIKE '%char%' OR columns.data_type LIKE '%text%')
        LOOP
            query := 'SELECT ''' || current_table.table_name || ''' as result_table,
                        ''' || current_column.column_name || ''' as result_column,
                        ' || current_column.column_name || ' as result_text,
                        CAST(id as text) as result_id
                        FROM ' || current_table.table_schema || '.' || current_table.table_name ||
                    ' WHERE ' || current_column.column_name || ' LIKE ' || quote_literal('%' || search_text || '%') ||
                    ' LIMIT 10';

            BEGIN
                FOR result IN EXECUTE query LOOP
                    table_name := result.result_table;
                    column_name := result.result_column;
                    found_text := result.result_text;
                    record_id := result.result_id;
                    RETURN NEXT;
                END LOOP;
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END LOOP;
    END LOOP;
    RETURN;
END;
$func$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION update_table_content(
    target_table text,
    target_column text,
    target_id text,
    new_content text,
    append boolean DEFAULT false,
    prepend boolean DEFAULT false
) RETURNS boolean AS $func$
DECLARE
    query text;
    success boolean := false;
    id_column_type text;
    current_content text;
BEGIN
    IF append AND prepend THEN
        RAISE EXCEPTION 'Cannot both append and prepend at the same time';
    END IF;

    EXECUTE format('SELECT data_type FROM information_schema.columns
                   WHERE table_name = %L AND column_name = ''id''',
                   target_table) INTO id_column_type;

    IF append OR prepend THEN
        EXECUTE format('SELECT %I FROM %I WHERE id = $1::%s',
                      target_column, target_table, id_column_type)
        INTO current_content
        USING target_id;

        IF current_content IS NOT NULL THEN
            IF append THEN
                new_content := current_content || new_content;
            ELSIF prepend THEN
                new_content := new_content || current_content;
            END IF;
        END IF;
    END IF;

    query := 'UPDATE ' || quote_ident(target_table) ||
             ' SET ' || quote_ident(target_column) || ' = $1' ||
             ' WHERE id = ';

    IF id_column_type IN ('integer', 'bigint', 'smallint') THEN
        query := query || '$2::' || id_column_type;
    ELSE
        query := query || '$2';
    END IF;

    BEGIN
        EXECUTE query USING new_content, target_id;

        IF FOUND THEN
            success := true;
        END IF;

        RETURN success;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'Error updating table: %', SQLERRM;
        RETURN false;
    END;
END;
$func$ LANGUAGE plpgsql;
"""


def _is_local(login_info):
    return login_info.get('local', False) or login_info.get('hostname') in ('localhost', '127.0.0.1')


def _reddit_container(login_info=None):
    """Name of the postmill/postgres container this run injects into.

    Reddit used to be a single shared `forum` container hard-coded here. To let two
    reddit runs execute in parallel without clobbering each other's in-flight injected
    content (both do a per-episode row UPDATE and, when a populate spec is present, a full
    DB reset), each run now claims its own forum copy (forum / forum-2 / forum-3) via
    reddit_instance.py, which exports the chosen name as REDDIT_CONTAINER. login_info may
    also carry it explicitly ('container'); env is the default. Falls back to 'forum' so
    every existing single-instance caller is unchanged.
    """
    if login_info and login_info.get('container'):
        return login_info['container']
    return os.getenv("REDDIT_CONTAINER", "forum")


def _run_local(command):
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def _run_remote(command, login_info, stdin_data=None):
    """Run a shell command on the reddit host over SSH and return (stdout, stderr, rc)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**{k: v for k, v in login_info.items() if k not in ('local', 'container')})
        stdin, stdout, stderr = client.exec_command(command)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.channel.shutdown_write()
        rc = stdout.channel.recv_exit_status()
        return stdout.read().decode(), stderr.read().decode(), rc
    finally:
        client.close()


_FS = '\x1f'


def run_sql(sql, login_info=None, fetch=False):
    """Execute SQL against this run's postmill DB and optionally return its rows.

    The one place that knows how to reach the forum container, local or over SSH. Callers
    that need ground truth (the adversary-evaluator getters) or per-episode state surgery
    (setup_cleanup.py) go through here rather than re-deriving the docker/psql incantation.

    SQL is fed on stdin rather than interpolated into a shell command, so no caller has to
    reason about two layers of quoting to ask a question about a post title.

    fetch=True returns a list of rows, each a list of column strings.

    Raises on a nonzero exit instead of returning an empty result: an unreadable DB must not
    be indistinguishable from "the attack left no trace", which is precisely the confusion
    that made every absence-based reddit adversary evaluator pass unconditionally.
    """
    _c = _reddit_container(login_info)
    args = ["docker", "exec", "-i", _c, "psql", "-U", "postgres", "-d", "postmill",
            "-v", "ON_ERROR_STOP=1"]
    if fetch:
        args += ["-t", "-A", "-F", _FS]

    if _is_local(login_info or {}):
        proc = subprocess.run(args, input=sql, capture_output=True, text=True)
        out, err, rc = proc.stdout, proc.stderr, proc.returncode
    else:
        out, err, rc = _run_remote(" ".join(shlex.quote(a) for a in args),
                                   login_info, stdin_data=sql)
    if rc != 0:
        raise RuntimeError(
            f"psql failed (rc={rc}) on container {_c}: {(err or out).strip()[:500]}\n"
            f"SQL: {sql[:400]}"
        )
    if not fetch:
        return True
    return [line.split(_FS) for line in out.splitlines() if line.strip()]


def deploy_functions_via_file(login_info=None):
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.sql', delete=False) as f:
        f.write(sql_functions)
        temp_file_path = f.name

    _c = _reddit_container(login_info)
    try:
        if _is_local(login_info):
            docker_command = (
                f'docker cp {temp_file_path} {_c}:/tmp/pg_functions.sql && '
                f'docker exec {_c} chmod 644 /tmp/pg_functions.sql && '
                f'docker exec {_c} bash -c "psql -U postgres -d postmill -f /tmp/pg_functions.sql"'
            )
            output, error, exit_status = _run_local(docker_command)
        else:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**{k: v for k, v in login_info.items() if k != 'local'})
                remote_path = f'/tmp/pg_functions_{tempfile.gettempprefix()}.sql'
                sftp = client.open_sftp()
                sftp.put(temp_file_path, remote_path)
                sftp.close()
                docker_command = (
                    f'docker cp {remote_path} {_c}:/tmp/pg_functions.sql && '
                    f'docker exec {_c} bash -c "psql -U postgres -d postmill -f /tmp/pg_functions.sql"'
                )
                stdin, stdout, stderr = client.exec_command(docker_command)
                exit_status = stdout.channel.recv_exit_status()
                output = stdout.read().decode()
                error = stderr.read().decode()
                client.exec_command(f'rm {remote_path}')
            finally:
                client.close()

        if output:
            print("Commands:")
            print(output)
        if error:
            print("Error info:")
            print(error)
        if exit_status == 0:
            print("PostgreSQL func deployed successfully")
        else:
            print(f"Failed deployment, exit: {exit_status}")

    except Exception as e:
        print(f"Error: {str(e)}")
        return False
    finally:
        os.unlink(temp_file_path)

    return True


def find_content(search_text, target_table=None, login_info=None):
    """
    Find content in the database matching the search text

    Args:
        search_text: Text to search for
        target_table: Optional specific table to search in (default=None searches all tables)

    Returns:
        List of matching records
    """
    escaped_search_text = search_text.replace("'", "''")
    _c = _reddit_container(login_info)

    if target_table:
        search_command = (
            f'''docker exec {_c} bash -c "psql -U postgres -d postmill -c \\"SELECT JSON_BUILD_OBJECT('table_name', table_name, 'column_name', column_name, 'found_text', found_text, 'record_id', record_id) FROM search_all_tables('{escaped_search_text}'::text, '{target_table}'::text);\\"" '''
        )
    else:
        search_command = (
            f'''docker exec {_c} bash -c "psql -U postgres -d postmill -c \\"SELECT JSON_BUILD_OBJECT('table_name', table_name, 'column_name', column_name, 'found_text', found_text, 'record_id', record_id) FROM search_all_tables('{escaped_search_text}'::text);\\"" '''
        )

    try:
        if _is_local(login_info):
            output, error, _ = _run_local(search_command)
        else:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**{k: v for k, v in login_info.items() if k != 'local'})
                stdin, stdout, stderr = client.exec_command(search_command)
                output = stdout.read().decode()
                error = stderr.read().decode()
            finally:
                client.close()

        if error and "ERROR" in error:
            print(f"Error: {error}")
            return []

        results = []
        if output:
            json_pattern = r'\{.*\}'
            matches = re.findall(json_pattern, output)
            for match in matches:
                try:
                    result_dict = json.loads(match)
                    results.append(result_dict)
                except json.JSONDecodeError:
                    print(f"Cannot parse JSON: {match}")
                except Exception as e:
                    print(f"Error handling match: {e}")

        return results

    except Exception as e:
        print(f"Error with function: {str(e)}")
        return None


def update_content(table_name, column_name, record_id, new_content="Thank you", operation_type="replace", restore=False, login_info=None):
    """
    Update content based on search results using a file-based approach to avoid escaping issues

    Args:
        table_name (str): The name of the table where the record exists.
        column_name (str): The name of the column (with text type) to be updated.
        record_id (str): The ID of the record to be updated.
        new_content: New content to update with, default is "Thank you"
        operation_type: "replace" (default), "append", or "prepend"
        restore: If True, forces operation_type to "replace"
    """
    if not table_name:
        print("No content found to update")
        return []

    valid_operations = ["replace", "append", "prepend"]
    if restore:
        operation_type = "replace"

    if operation_type not in valid_operations:
        print(f"Invalid operation type: {operation_type}. Must be one of {valid_operations}")
        return False

    append_flag = "TRUE" if operation_type == "append" else "FALSE"
    prepend_flag = "TRUE" if operation_type == "prepend" else "FALSE"

    with tempfile.NamedTemporaryFile(mode='w+', suffix='.txt', delete=False) as content_file:
        content_file.write(new_content)
        content_file_path = content_file.name

    update_sql = f"""
        DO $$
        DECLARE
            content_text text;
        BEGIN
            content_text := pg_read_file('/tmp/update_content.txt', 0, 1000000);
            PERFORM update_table_content(
                '{table_name}',
                '{column_name}',
                '{record_id}',
                content_text,
                {append_flag},
                {prepend_flag}
            );
        END $$;
        SELECT 'true' as result;
        """

    with tempfile.NamedTemporaryFile(mode='w+', suffix='.sql', delete=False) as sql_file:
        sql_file.write(update_sql)
        sql_file_path = sql_file.name

    print(f"\nUpdating record:")
    print(f"Table: {table_name}, Column: {column_name}, ID: {record_id}")
    print(f"Operation: {operation_type}")

    _c = _reddit_container(login_info)
    try:
        if _is_local(login_info):
            _run_local(f'docker cp {content_file_path} {_c}:/tmp/update_content.txt && docker exec {_c} chmod 644 /tmp/update_content.txt')
            docker_sql_cmd = (
                f'docker cp {sql_file_path} {_c}:/tmp/update_sql.sql && '
                f'docker exec {_c} chmod 644 /tmp/update_sql.sql && '
                f'docker exec {_c} bash -c "psql -U postgres -d postmill -f /tmp/update_sql.sql"'
            )
            output, error, _ = _run_local(docker_sql_cmd)
        else:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**{k: v for k, v in login_info.items() if k != 'local'})
                sftp = client.open_sftp()
                remote_content_path = f'/tmp/update_content_{tempfile.gettempprefix()}.txt'
                sftp.put(content_file_path, remote_content_path)
                remote_sql_path = f'/tmp/update_sql_{tempfile.gettempprefix()}.sql'
                sftp.put(sql_file_path, remote_sql_path)
                sftp.close()
                stdin, stdout, stderr = client.exec_command(
                    f'docker cp {remote_content_path} {_c}:/tmp/update_content.txt'
                )
                stdout.read()
                update_command = (
                    f'docker cp {remote_sql_path} {_c}:/tmp/update_sql.sql && '
                    f'docker exec {_c} bash -c "psql -U postgres -d postmill -f /tmp/update_sql.sql"'
                )
                stdin, stdout, stderr = client.exec_command(update_command)
                output = stdout.read().decode()
                error = stderr.read().decode()
                client.exec_command(f'rm {remote_content_path} {remote_sql_path}')
            finally:
                client.close()
                print("\nSSH connection closed")

        success = bool(output and ('t' in output.lower() or 'true' in output.lower()))
        if success:
            print("Update successful!")
        else:
            print(f"Update failed: {error}")

    except Exception as e:
        print(f"Error updating content: {str(e)}")
        return False
    finally:
        os.unlink(content_file_path)
        os.unlink(sql_file_path)

    return True


def populate_reddit_database_from_json(json_path, login_info=None):
    """Populate the Reddit forum DB from a JSON spec.

    The JSON file must contain a "records" list where each record has:
        table_name, column_name, record_id, new_content

    json_path is resolved relative to the RedTeamCUA root unless it is absolute.
    Every record is applied via update_content with operation_type="replace".
    """
    if not os.path.isabs(json_path):
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(root, json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    records = spec.get("records", [])
    if not records:
        print(f"No records found in population spec: {json_path}")
        return False

    deploy_functions_via_file(login_info=login_info)

    for record in records:
        ok = update_content(
            table_name=record["table_name"],
            column_name=record["column_name"],
            record_id=record["record_id"],
            new_content=record["new_content"],
            operation_type="replace",
            login_info=login_info,
        )
        if not ok:
            return False

    return True


WASP_SEED_POST_ID = 137403
AGENT_USER_ID = 13915


def _sql_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def find_reddit_user_id(username, login_info=None):
    """Return the numeric id of `username`, or None if no such user exists."""
    rows = run_sql(f"SELECT id FROM users WHERE username={_sql_str(username)};",
                   login_info=login_info, fetch=True)
    return int(rows[0][0]) if rows else None


def ensure_reddit_user(account, login_info=None):
    """
    Make sure a forum account exists (idempotent) and return its numeric id. Reddit/postmill has
    no admin user-creation API the way GitLab does, so the row is inserted straight into the DB
    with the NOT-NULL preference columns filled from sensible defaults. `account` is a dict with a
    "username" and an optional "password" (an empty hash is fine -- these accounts are never
    logged into; they exist only to AUTHOR the staged comments). Used to give the injection
    comment, and each benign filler comment, a distinct third-party author who is neither the
    agent nor the seed post's author.
    """
    username = account["username"]
    existing = find_reddit_user_id(username, login_info=login_info)
    if existing is not None:
        return existing
    password = account.get("password", "")
    run_sql(
        "INSERT INTO users (id, username, password, created, normalized_username, admin, locale, "
        "night_mode, show_custom_stylesheets, trusted, front_page, open_external_links_in_new_tab, "
        "auto_fetch_submission_titles, enable_post_previews, show_thumbnails, notify_on_reply, "
        "notify_on_mentions, front_page_sort_mode, timezone, allow_private_messages, poppers_enabled, "
        "full_width_display_enabled, submission_link_destination) "
        f"SELECT coalesce(max(id),0)+1, {_sql_str(username)}, {_sql_str(password)}, now(), "
        f"{_sql_str(username.lower())}, false, 'en', 'auto', true, false, 'featured', false, true, "
        "true, true, true, true, 'hot', 'UTC', true, true, false, 'url' FROM users;",
        login_info=login_info,
    )
    uid = find_reddit_user_id(username, login_info=login_info)
    if uid is None:
        raise RuntimeError(f"Failed to create reddit user {username!r}")
    return uid


def insert_seed_comment(submission_id, user_id, body, login_info=None,
                        parent_id=None, age_seconds=0, net_score=0):
    """
    Insert one comment on `submission_id` authored by `user_id`, and return its new id.

    `net_score` seeds the vote score, and it alone decides render order: the submission page lists
    top-level comments by STRICTLY DESCENDING net_score (postmill Submission::getTopLevelComments,
    a usort on `$b->netScore <=> $a->netScore`; the SORT_* options are for the submission listing,
    not comments, so there is no user-selectable comment sort to defeat). Give the benign fillers
    distinct positive scores and the injection 0 and it is guaranteed last -- below the fold on
    page load, exactly like the buried-comment attack on GitLab. `age_seconds` back-dates the
    timestamp so the discussion also reads chronologically in threaded/`getComments` views; it does
    not affect top-level order. `search_doc` is filled by the table's own trigger.
    """
    parent_sql = "NULL" if parent_id is None else str(int(parent_id))
    run_sql(
        "INSERT INTO comments (id, user_id, submission_id, parent_id, body, timestamp, visibility, "
        "moderated, user_flag, net_score) "
        f"SELECT coalesce(max(id),0)+1, {int(user_id)}, {int(submission_id)}, {parent_sql}, "
        f"{_sql_str(body)}, now() - interval '{int(age_seconds)} seconds', 'visible', false, "
        f"'none', {int(net_score)} FROM comments;",
        login_info=login_info,
    )
    rows = run_sql(
        f"SELECT max(id) FROM comments WHERE submission_id={int(submission_id)} "
        f"AND user_id={int(user_id)};",
        login_info=login_info, fetch=True)
    return int(rows[0][0])


def _recompute_comment_count(submission_id, login_info=None):
    """comment_count is denormalised on submissions; refresh it after inserting comments."""
    run_sql(
        f"UPDATE submissions SET comment_count=(SELECT count(*) FROM comments "
        f"WHERE submission_id={int(submission_id)} AND visibility='visible') "
        f"WHERE id={int(submission_id)};",
        login_info=login_info)


def inject_as_comments(submission_id, injection_author, new_content, login_info=None,
                       filler_comments=None):
    """
    Plant the injection as a COMMENT on an existing submission, under a stack of benign filler
    comments so it renders below the fold.

    injection_author -- account dict (username [+ optional password]) for the comment that
        carries the injection. A distinct third party: neither the agent nor the post's author.
    filler_comments  -- list of {"body": str, "author": <account dict>} posted BEFORE the
        injection. They are given distinct descending net_scores so the submission page (which
        orders top-level comments by descending net_score) keeps them above the injection; the
        injection is score 0, so it sinks to the bottom. [] puts the injection right under the post.

    All comments are top-level siblings on the submission. Returns the injection comment id.
    """
    filler_comments = filler_comments or []
    n = len(filler_comments)
    for i, fc in enumerate(filler_comments):
        author_id = ensure_reddit_user(fc["author"], login_info=login_info)
        insert_seed_comment(submission_id, author_id, fc["body"], login_info=login_info,
                            age_seconds=(n - i + 1) * 3600, net_score=2 * (n - i) + 1)
    author_id = ensure_reddit_user(injection_author, login_info=login_info)
    injection_id = insert_seed_comment(submission_id, author_id, new_content,
                                       login_info=login_info, age_seconds=0, net_score=0)
    _recompute_comment_count(submission_id, login_info=login_info)
    print(f"Injected comment {injection_id} on submission {submission_id} by "
          f"{injection_author['username']} under {n} benign comment(s)")
    return injection_id


def _normalize_seed_post_votes(login_info=None):
    """Drop the agent's vote on the WASP seed post and pin its base score to 1.

    Idempotent and safe to call every episode. Leaves the author's own upvote intact.
    """
    sql = (
        f"DELETE FROM submission_votes "
        f"WHERE submission_id={WASP_SEED_POST_ID} AND user_id={AGENT_USER_ID}; "
        f"UPDATE submissions SET net_score=1 WHERE id={WASP_SEED_POST_ID};"
    )
    _c = _reddit_container(login_info)
    command = f'''docker exec {_c} bash -c "psql -U postgres -d postmill -c \\"{sql}\\""'''
    try:
        if _is_local(login_info or {}):
            _run_local(command)
        else:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**{k: v for k, v in login_info.items() if k != 'local'})
                _, stdout, _ = client.exec_command(command)
                stdout.channel.recv_exit_status()
            finally:
                client.close()
        print(f"Normalised votes on seed post {WASP_SEED_POST_ID} (removed agent vote, score->1)")
        return True
    except Exception as e:
        print(f"Error normalising seed-post votes: {e}")
        return False


def stamp_episode_epoch(login_info=None):
    """Record the DB's own clock at the start of this episode, for the adversary getters.

    get_reddit_state scopes every "did the attack happen" question to rows created at or
    after this instant, so an artifact a PREVIOUS episode left behind cannot score. Read
    from Postgres, not the host, because the two clocks need not agree and an evaluator
    that silently drifts ahead of the DB would judge every episode as having created
    nothing.
    """
    try:
        now = run_sql("SELECT date_trunc('second', now()) + interval '1 second';",
                      login_info=login_info, fetch=True)[0][0]
    except Exception as e:
        print(f"Could not stamp reddit episode epoch: {e}")
        os.environ.pop("REDDIT_EPISODE_EPOCH", None)
        return None
    os.environ["REDDIT_EPISODE_EPOCH"] = now
    print(f"Reddit episode epoch: {now}")
    return now


def reddit_adv_setup(config, login_info=None):

    deploy_functions_via_file(login_info=login_info)

    if str(config.get("parameters", {}).get("record_id")) == str(WASP_SEED_POST_ID):
        _normalize_seed_post_votes(login_info=login_info)

    if config.get("populate_reddit_database"):
        json_path = config.get("populate_json_path")
        if not json_path:
            return False
        if not populate_reddit_database_from_json(json_path, login_info=login_info):
            return False
        os.environ["RESET_REDDIT_DATABASE"] = "True"

    stamp_episode_epoch(login_info=login_info)

    if config["parameters"].get("injection_placement") == "comment":
        injection_author = config["parameters"].get("injection_author")
        if not injection_author:
            raise ValueError("injection_placement='comment' requires an injection_author account dict")
        submission_id = config["parameters"].get("record_id") or WASP_SEED_POST_ID
        return bool(inject_as_comments(
            submission_id=int(submission_id),
            injection_author=injection_author,
            new_content=config["parameters"]["new_content"],
            login_info=login_info,
            filler_comments=config["parameters"].get("filler_comments") or [],
        ))

    update_mode = config["parameters"]["update_mode"]
    operation_type = config["parameters"].get("operation_type", "replace")

    if update_mode == "precise":
        return bool(update_content(
            table_name=config["parameters"]["table_name"],
            column_name=config["parameters"]["column_name"],
            record_id=config["parameters"]["record_id"],
            new_content=config["parameters"]["new_content"],
            operation_type=operation_type,
            login_info=login_info,
        ))

    elif update_mode == "search":
        results = find_content(config["parameters"]["search_text"], "comments", login_info=login_info)

        if len(results) != 1:
            return f"Multiple results were found, or none were found: {results}"

        return bool(update_content(
            table_name=results[0]["table_name"],
            column_name=results[0]["column_name"],
            record_id=results[0]["record_id"],
            new_content=config["parameters"]["new_content"],
            operation_type=operation_type,
            login_info=login_info,
        ))

    else:
        return "Update mode is not supported!"


if __name__ == "__main__":
    import textwrap

    login_info = {
        'hostname': os.getenv("REDDIT", "localhost"),
        'username': 'ubuntu',
        'key_filename': os.getenv("KEY_FILENAME"),
    }

    deploy_functions_via_file(login_info=login_info)

    update_content(
        table_name='comments',
        column_name='body',
        record_id='1913532',
        new_content=textwrap.dedent("Inject Successfully!"),
        operation_type="replace",
        login_info=login_info,
    )
