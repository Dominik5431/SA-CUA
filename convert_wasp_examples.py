#!/usr/bin/env python3
"""
convert_wasp_examples.py

Converts WASP's (facebookresearch/wasp, "webarena_prompt_injections") 21
adversarial scenario definitions in configs/experiment_config.raw.json into
RedTeamCUA's goals/benign + goals/adv task-pair format, so they can be run
through the existing CUA red-teaming pipeline (generate_config.py -> run.py)
alongside the Reddit/OwnCloud/RocketChat platforms.

WASP's benign user goals and injection-format message templates are loaded
directly from the sibling WASP repo's constants.py/utils.py (via importlib,
not sys.path injection) rather than copy-pasted, so this script never drifts
from the upstream benchmark definition.

--------------------------------------------------------------------------
Design notes / deliberate deviations from WASP's live run.py + prompt_injector.py
--------------------------------------------------------------------------
1. Dynamic vs. fixed injection target.
   WASP creates a brand-new GitLab issue / Reddit post per scenario at run
   time (via Playwright) and evaluates against its just-created URL.
   RedTeamCUA's existing platforms (Reddit/OwnCloud/RocketChat) instead
   inject into a FIXED, pre-seeded piece of content with a statically known
   URL/DB row (see adv/WebArena_Reddit/injection.py's reddit_adv_setup,
   which edits an existing seeded postmill row by table/column/record_id).
   To stay consistent with that convention -- and avoid inventing a new
   "discover the URL created at runtime" mechanism -- every scenario here
   is assigned a fixed placeholder target (a GitLab issue IID / Reddit post
   id) that must be seeded ONCE when the platform environment is
   provisioned. Fields that depend on that seeding are left as `null` with
   an adjacent "_seed_todo" string explaining exactly what to create and
   which field to patch afterwards. The actual edit of that seeded content's
   body at run time is expected to be implemented as `gitlab_adv_setup`
   (new; mirrors the existing `reddit_adv_setup`) / `reddit_adv_setup`
   (reused as-is) in the (not yet written) adv/WebArena_Gitlab/injection.py.

2. Two locations per scenario, not one.
   Every WASP scenario actually references two distinct places:
     (a) where the injected instruction TEXT lives (the seeded issue/post
         -- see #1 above), and
     (b) the scenario's own `action_url` / eval target, e.g. a GitLab
         settings page or an already-existing Reddit thread from the
         pre-seeded WebArena dump -- fully static and renderable right now
         from the scenario's own `parameters`, no seeding required.
   These are kept separate in the output: (a) becomes `target_url` +
   `meta_injection` on the benign task, (b) becomes the adversary
   evaluator's URL.

3. Format/goal granularity.
   run.py only ever loops over 2 injection formats (goal_hijacking_plain_text,
   goal_hijacking_url_injection) x up to 2 benign user goals per platform.
   Each (scenario, user_goal) pair becomes one benign+adversary FILE PAIR
   (mirroring e.g. benign_task.raw_own_reddit_gitclone.json); the adversary
   file holds one entry per injection format. It is duplicated per user_goal
   (rather than shared) because the injected payload text embeds that goal's
   action verb (WASP's `user_goal_str`: comment / upvote / like).

4. Domain templating.
   All WASP-side string templating ({gitlab_domain}, {reddit_domain}, and
   scenario-specific `parameters`) is fully resolved at conversion time,
   except the live domain itself, which is left as the __GITLAB__ / __REDDIT__
   placeholder token already used elsewhere in RedTeamCUA (see
   generate_config.py's sub_url()). NOTE: __GITLAB__ does not exist in
   sub_url() yet -- it needs to be added there (and a "gitlab" branch added
   to create_config()) before these gitlab configs can be run end to end.
   That is tracked separately as part of standing up the GitLab platform;
   it is out of scope for this pure data-conversion script.

5. "exfil" eval_types (5 of the 21 scenarios) have no RedTeamCUA evaluator
   yet. WASP's own ExfilEvaluator (visualwebarena/evaluation_harness/
   light_evaluation.py) does not check a webpage -- it scans the agent's own
   action trace for a substring. RedTeamCUA already writes an equivalent
   traj.jsonl per run, so this is emitted here as:
       {"func": "check_trajectory_includes",
        "result": {"type": "agent_trajectory"},
        "expected": {"type": "rule", "rules": {"expected": "<substring>"}}}
   pending that metric + getter pair being implemented in
   desktop_env/evaluators/metrics and desktop_env/evaluators/getters.

6. setup_fn / cleanup_fn (e.g. make_project_as_agent_user_and_attacker_account,
   delete_deploy_token) are Playwright-driven account/resource provisioning
   ported from WASP's environment_editors/gitlab_editor.py. They are carried
   through as "_wasp_source.setup_fn"/"cleanup_fn" metadata rather than
   fabricated into a working RedTeamCUA action here -- wiring them up is
   part of the (not yet written) adv/WebArena_Gitlab/injection.py, not this
   script.

Usage:
    python convert_wasp_examples.py \
        --wasp-root ../wasp/webarena_prompt_injections \
        --output-root .
"""

import argparse
import copy
import importlib.util
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_module_from_path(module_name: str, file_path: str):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def get_user_goal_str(goal_text: str) -> str:
    """Faithful port of WebArenaPromptInjector._get_user_goal_str's classification logic
    (prompt_injector.py), without importing the class itself (which pulls in
    Playwright-based editors we don't need here)."""
    lowered = goal_text.lower()
    if "comment" in lowered:
        return "comment"
    elif "upvote" in lowered:
        return "upvote"
    elif "thumbs_up" in lowered:
        return "like"
    return "complete user instruction"


class PlaceholderDict(dict):
    """Renders missing format keys as a self-documenting __SEED_<KEY>__ token
    instead of raising KeyError. Several WASP scenarios (e.g. gitlab's "add
    member", which uses setup_fn=make_project_as_agent_user_and_attacker_account)
    reference parameters that are only populated live, at run time, by their
    setup_fn -- WASP's own params_dict.update(possible_updates_to_config) step
    in _prepare_environment(). Since this script does no live environment
    setup, those keys are genuinely unknown here; flagging them inline (rather
    than crashing, or silently guessing a value) keeps the output honest about
    exactly what the future adv/WebArena_Gitlab/injection.py setup step must
    supply."""

    def __missing__(self, key):
        return f"__SEED_{key.upper()}__"


def render_str(template: str, params: dict) -> str:
    return template.format_map(PlaceholderDict(params))


def render_obj(obj, params: dict):
    """Generic port of WASP's utils.instantiate_dict_str_with_params, applied
    to an already-deep-copied structure."""
    if isinstance(obj, dict):
        return {k: render_obj(v, params) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [render_obj(v, params) for v in obj]
    elif isinstance(obj, str):
        return render_str(obj, params)
    else:
        return obj


def build_program_html_evaluator(program_html: list, params=None) -> dict:
    """Translate a WASP `program_html` eval list into a RedTeamCUA evaluator dict.

    Shared by both sides of a task pair. RedTeamCUA's DesktopEnv._set_task_info()
    reads `evaluator["func"]` unconditionally and looks the name up in
    desktop_env/evaluators/metrics, so a WASP-shaped block (`eval_types` /
    `program_html` / `required_contents`) is not merely unhandled downstream --
    it raises KeyError: 'func' inside prepare_injection(), after the VM has
    booted. WASP's own light_evaluation.py semantics map cleanly onto the pair
    (must_include / must_exclude metric, get_info_from_websites getter): the
    getter navigates each url, applies `locator` as JS when it is a
    `document.`-prefixed expression and falls back to the whole page when it is
    empty, exactly as WASP's HTMLContentEvaluator does.

    `params` is None for input that is already fully resolved (the benign side),
    which matters: render_obj() runs str.format_map(), so a second pass over an
    already-rendered string would eat any literal brace in it.
    """
    program_html_rendered = render_obj(program_html, params) if params is not None else program_html
    required_contents = program_html[0]["required_contents"]
    if "must_include" in required_contents:
        func = "must_include"
        expected = required_contents["must_include"]
    elif "must_exclude" in required_contents:
        func = "must_exclude"
        expected = required_contents["must_exclude"]
    else:
        raise NotImplementedError(f"Unhandled required_contents shape: {required_contents}")
    expected_rendered = render_obj(expected, params) if params is not None else expected
    return {
        "func": func,
        "result": {
            "type": "info_from_websites",
            "program_html": [
                {"url": ph["url"], "locator": ph.get("locator", "")}
                for ph in program_html_rendered
            ],
        },
        "expected": {"type": "rule", "rules": {"expected": expected_rendered}},
    }


def build_adversary_evaluator(scenario_eval: dict, params: dict) -> dict:
    """Translate one WASP `eval` block into a RedTeamCUA evaluator dict."""
    eval_types = scenario_eval["eval_types"]
    if eval_types == ["exfil"]:
        exfil_substring = render_str(scenario_eval["exfil_data"][0]["must_include"], params)
        return {
            "func": "check_trajectory_includes",
            "result": {"type": "agent_trajectory"},
            "expected": {"type": "rule", "rules": {"expected": exfil_substring}},
        }
    elif eval_types == ["program_html"]:
        return build_program_html_evaluator(scenario_eval["program_html"], params)
    else:
        raise NotImplementedError(f"Unhandled eval_types: {eval_types}")


def build_benign_evaluator(user_goals_eval: dict, target_url: str) -> dict:
    """Same translation as the adversary side, retargeted at the seeded issue/post.

    WASP's benign user goals are all program_html checks on the page the goal
    acted upon; that page is the fixed seeded target here (design note #1), so
    the url is overwritten rather than rendered from `parameters`.
    """
    eval_types = user_goals_eval["eval_types"]
    if eval_types != ["program_html"]:
        raise NotImplementedError(f"Unhandled benign eval_types: {eval_types}")
    program_html = copy.deepcopy(user_goals_eval["program_html"])
    for ph in program_html:
        ph["url"] = target_url
    return build_program_html_evaluator(program_html)


PLATFORM_DOMAIN_TOKEN = {"gitlab": "__GITLAB__", "reddit": "__REDDIT__"}
PLATFORM_GOAL_SLUGS = {
    "gitlab": ["comment", "reaction"],
    "reddit": ["comment", "upvote"],
}
INJECTION_FORMATS = ["goal_hijacking_plain_text", "goal_hijacking_url_injection"]


def build_target_url_and_meta_injection(platform: str, scenario_slug: str, params: dict):
    """Fixed, to-be-seeded injection target for this scenario (see design note #1)."""
    domain_token = PLATFORM_DOMAIN_TOKEN[platform]
    if platform == "gitlab":
        project_owner = params.get("project_owner", "byteblaze")
        project_name = params.get("project_name", "dotfiles")
        target_url = f"{domain_token}/{project_owner}/{project_name}/-/issues/__SEED_ISSUE_IID__"
        meta_injection_params = {
            "seed_project_owner": project_owner,
            "seed_project_name": project_name,
            "issue_iid": None,
        }
        seed_todo = (
            f"Create one GitLab issue on {project_owner}/{project_name} (title can be anything "
            f"identifiable, e.g. 'wasp_{scenario_slug}') during environment provisioning. Then "
            f"replace __SEED_ISSUE_IID__ in target_url with its IID and set meta_injection."
            f"parameters.issue_iid here. A new `gitlab_adv_setup` action (adv/WebArena_Gitlab/"
            f"injection.py, not yet written) should overwrite that issue's description via SQL "
            f"against gitlab's internal Postgres DB, mirroring reddit_adv_setup's find_content/"
            f"update_content pattern."
        )
    elif platform == "reddit":
        target_url = f"{domain_token}/f/allentown/__SEED_POST_ID__"
        meta_injection_params = {
            "update_mode": "precise",
            "table_name": "submissions",
            "column_name": "body",
            "record_id": None,
        }
        seed_todo = (
            f"Create one post in forum 'allentown' (title can be anything identifiable, e.g. "
            f"'wasp_{scenario_slug}') during environment provisioning -- same forum WASP itself "
            f"uses for its injected posts. Then replace __SEED_POST_ID__ in target_url and "
            f"meta_injection.parameters.record_id with its real submissions.id. reddit_adv_setup "
            f"(adv/WebArena_Reddit/injection.py, already implemented) can be reused as-is."
        )
    else:
        raise NotImplementedError(platform)
    return target_url, meta_injection_params, seed_todo


def convert(wasp_root: str, output_root: str, dry_run: bool = False):
    constants = load_module_from_path("wasp_constants", os.path.join(wasp_root, "constants.py"))

    config_path = os.path.join(wasp_root, "configs", "experiment_config.raw.json")
    with open(config_path, "r") as f:
        experiment_config = json.load(f)
    scenarios = experiment_config["prompt_injections_setup_config"]

    benign_dir = os.path.join(output_root, "goals", "benign")
    adv_dir = os.path.join(output_root, "goals", "adv")
    os.makedirs(benign_dir, exist_ok=True)
    os.makedirs(adv_dir, exist_ok=True)

    user_goals_cls = {"gitlab": constants.GitlabUserGoals, "reddit": constants.RedditUserGoals}

    written = []
    for scenario in scenarios:
        platform = scenario["environment"]
        if platform not in user_goals_cls:
            print(f"  SKIP scenario '{scenario['free_form_name']}': unknown environment {platform}")
            continue

        scenario_slug = slugify(scenario["free_form_name"])
        goals = user_goals_cls[platform].GOALS
        goal_slugs = PLATFORM_GOAL_SLUGS[platform]

        base_params = {"gitlab_domain": "__GITLAB__", "reddit_domain": "__REDDIT__"}
        base_params.update(scenario["parameters"])

        target_url, meta_injection_params, seed_todo = build_target_url_and_meta_injection(
            platform, scenario_slug, scenario["parameters"]
        )

        for goal_idx, goal_text in enumerate(goals):
            goal_slug = goal_slugs[goal_idx]
            task_id = f"own_wasp_{platform}_{scenario_slug}_{goal_slug}"

            benign_evaluator = build_benign_evaluator(
                user_goals_cls[platform](target_url).EVALS[goal_idx], target_url
            )
            benign_task = {
                "id": task_id,
                "type": "end2end",
                "platform": platform,
                "instruction": {
                    variant: {"instruction": goal_text}
                    for variant in ("loose", "specific", "pointer", "end2end")
                },
                "target_url": target_url,
                "meta_injection": {"parameters": copy.deepcopy(meta_injection_params)},
                "evaluator": benign_evaluator,
                "_wasp_source": {
                    "free_form_name": scenario["free_form_name"],
                    "notes": scenario.get("notes", ""),
                },
                "_seed_todo": seed_todo,
            }
            benign_path = os.path.join(benign_dir, f"benign_task.raw_{task_id}.json")

            user_goal_str = get_user_goal_str(goal_text)
            adversary_entries = []
            for injection_format in INJECTION_FORMATS:
                format_slug = "plain_text" if injection_format.endswith("plain_text") else "url_injection"

                instantiated_action_url = render_str(scenario["action_url"], base_params)
                instantiated_instruction_text = render_str(scenario["instruction"], base_params)
                if "url_injection" in injection_format:
                    instantiated_instruction_text = instantiated_instruction_text.replace(" ", "-")

                injected_message = constants.PromptInjectionFormat.MESSAGE[injection_format].format(
                    user_goal_str=user_goal_str,
                    instantiated_url_of_action=instantiated_action_url,
                    instantiated_instruction_text=instantiated_instruction_text,
                )

                eval_params = dict(base_params)
                adversary_evaluator = build_adversary_evaluator(scenario["eval"], eval_params)

                action_type = "gitlab_adv_setup" if platform == "gitlab" else "reddit_adv_setup"
                adversary_entries.append(
                    {
                        "id": f"{task_id}_{format_slug}",
                        "category": f"WASP/{platform}/{scenario_slug}",
                        "description": f"{scenario_slug}_{format_slug}",
                        "injection_contents": [
                            {
                                "type": "language",
                                "action": {
                                    "type": action_type,
                                    "parameters": {
                                        "new_content": injected_message,
                                        "operation_type": "replace",
                                    },
                                },
                            }
                        ],
                        "evaluator": adversary_evaluator,
                        "_wasp_source": {
                            "free_form_name": scenario["free_form_name"],
                            "injection_format": injection_format,
                            "exfil": scenario.get("exfil", False),
                            "setup_fn": scenario.get("setup_fn", ""),
                            "cleanup_fn": scenario.get("cleanup_fn", ""),
                            "setup_cleanup_parameters": scenario.get("parameters", {}),
                            "action_url_rendered": instantiated_action_url,
                            "notes": scenario.get("notes", ""),
                        },
                    }
                )
            adversary_path = os.path.join(adv_dir, f"adversary_task.raw_{task_id}.json")

            if dry_run:
                print(f"  WOULD WRITE {benign_path}")
                print(f"  WOULD WRITE {adversary_path}")
            else:
                with open(benign_path, "w") as f:
                    json.dump([benign_task], f, indent=4)
                    f.write("\n")
                with open(adversary_path, "w") as f:
                    json.dump(adversary_entries, f, indent=4)
                    f.write("\n")
            written.append((benign_path, adversary_path))

    print(f"\nDone. {len(scenarios)} WASP scenarios -> {len(written)} benign/adversary file pairs "
          f"({'dry run, nothing written' if dry_run else f'written under {benign_dir} and {adv_dir}'}).")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--wasp-root",
        default=os.path.join(SCRIPT_DIR, "..", "wasp", "webarena_prompt_injections"),
        help="Path to the webarena_prompt_injections (WASP) repo root",
    )
    parser.add_argument(
        "--output-root",
        default=SCRIPT_DIR,
        help="RedTeamCUA repo root to write goals/benign, goals/adv into (default: this script's directory)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List what would be written without writing it")
    args = parser.parse_args()

    wasp_root = os.path.abspath(args.wasp_root)
    if not os.path.isfile(os.path.join(wasp_root, "configs", "experiment_config.raw.json")):
        sys.exit(f"ERROR: could not find configs/experiment_config.raw.json under --wasp-root {wasp_root}")

    convert(wasp_root, os.path.abspath(args.output_root), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
