"""
Playwright-driven GitLab automation, ported from WASP's (facebookresearch/wasp,
"webarena_prompt_injections") environment_editors/gitlab_editor.py against the same
standard WebArena GitLab image (gitlab-populated-final-port8023) RedTeamCUA runs as its
"gitlab" container. Only the methods actually needed by setup_cleanup.py's Group-B
setup_fn/cleanup_fn dispatch (the 6 GitLab WASP scenarios that need dynamic per-run state)
are ported -- not WASP's full editor (issue/branch/merge-request creation, etc. are unused
here since none of RedTeamCUA's setup_fn/cleanup_fn names reference them).

Selectors are copied verbatim from the WASP source since they target a specific, pinned
GitLab UI version baked into that image -- they are inherently fragile and are expected to
need re-verification if/when the underlying image changes.
"""
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


class WebArenaEditorException(Exception):
    pass


class GitlabEditor:
    """Automates actions on GitLab through a headless browser using Playwright.

    Usage:
        with GitlabEditor(gitlab_domain) as editor:
            editor.login(username, password)
            editor.create_private_project(...)
    """

    def __init__(self, gitlab_domain: str, headless: bool = True):
        self.gitlab_domain = gitlab_domain.rstrip("/")
        self.headless = headless

    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        return self

    def close(self):
        if hasattr(self, "page") and self.page:
            self.page.close()
        if hasattr(self, "context") and self.context:
            self.context.close()
        if hasattr(self, "browser") and self.browser:
            self.browser.close()
        if hasattr(self, "playwright") and self.playwright:
            self.playwright.stop()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


    def login(self, username: str, password: str) -> None:
        self.page.goto(f"{self.gitlab_domain}/users/sign_in", wait_until="networkidle")
        self.page.set_viewport_size({"width": 1280, "height": 1500})

        try:
            self.page.wait_for_selector("input#user_login", timeout=10000)
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(
                f"Failed to login. Did not find user input field at {self.page.url}."
            )

        self.page.fill("input#user_login", username)
        self.page.fill("input#user_password", password)

        try:
            with self.page.expect_navigation():
                self.page.click('button[type="submit"]')
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Failed to login as {username}.")

        if "To personalize your GitLab experience" in self.page.content():
            try:
                self.page.select_option("#user_role", value="software_developer")
                with self.page.expect_navigation():
                    self.page.click('button:has-text("Get started!")')
            except Exception:
                pass

        if self.page.url.strip("/") != self.gitlab_domain:
            raise WebArenaEditorException(
                f"Failed to login as {username}; ended up at {self.page.url}."
            )


    def create_private_group(self, group_name: str) -> str:
        self.page.goto(f"{self.gitlab_domain}/groups/new#create-group-pane", wait_until="networkidle")

        self.page.locator("#group_name").wait_for(state="visible")
        self.page.locator("#group_name").fill(group_name)
        self.page.wait_for_timeout(3000)

        self.page.locator("label[for='group_visibility_level_0']").check()
        self.page.locator("#user_role").select_option("software_developer")
        self.page.locator("label[for='group_setup_for_company_true']").click()
        self.page.locator("#group_jobs_to_be_done").select_option("exploring")

        try:
            with self.page.expect_navigation():
                self.page.get_by_role("button", name="Create group").click()
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Failed to create group '{group_name}'; ended up at {self.page.url}.")

        parsed = urlparse(self.page.url)
        return parsed.path.strip("/").split("/")[-1]

    def create_private_project(self, project_name: str, namespace_name: str = None, timeout_for_dropdown: int = 5000) -> str:
        self.page.goto(f"{self.gitlab_domain}/projects/new#blank_project", wait_until="networkidle")

        name_locator = self.page.get_by_label("Project name")
        name_locator.wait_for(state="visible")
        name_locator.fill(project_name)
        self.page.wait_for_timeout(2000)

        namespace_dropdown_button_selector = "button#__BVID__16__BV_toggle_"
        dropdown_button = self.page.locator(namespace_dropdown_button_selector)

        if dropdown_button.is_visible():
            dropdown_button.click()
            self.page.wait_for_timeout(timeout_for_dropdown)
            group_item = self.page.locator(f"li.gl-dropdown-item >> text={namespace_name}")
            if not group_item.is_visible():
                raise WebArenaEditorException(
                    f"Unable to find namespace {namespace_name} in the dropdown; create it first."
                )
            group_item.click()
        elif namespace_name is not None:
            prefilled_url_element = self.page.query_selector(".input-group-prepend.static-namespace")
            prefilled_url = prefilled_url_element.get_attribute("title")
            prefilled_namespace_name = urlparse(prefilled_url).path.strip("/").split("/")[-1]
            if prefilled_namespace_name != namespace_name:
                raise WebArenaEditorException(
                    f"Namespace {namespace_name} requested but page is prefilled with {prefilled_namespace_name}."
                )

        self.page.locator("#blank-project-pane").get_by_text("PrivateProject access must be").check()

        try:
            with self.page.expect_navigation():
                self.page.get_by_role("button", name="Create project").click()
                if self.page.url.endswith("projects"):
                    raise WebArenaEditorException("Failed to end up at the expected URL")
        except (PlaywrightTimeoutError, WebArenaEditorException):
            error_container = self.page.locator("div.project-edit-errors")
            if error_container.count() > 0:
                errors = error_container.locator("ul li").all_inner_texts()
                error_message = "; ".join(errors)
                if "taken" in error_message:
                    print(f"[Info] Project {project_name} already exists; proceeding.")
                    return project_name
                raise WebArenaEditorException(f"Failed to create project '{project_name}': {error_message}")
            raise WebArenaEditorException(f"Failed to create project '{project_name}'; ended up at {self.page.url}.")

        parsed = urlparse(self.page.url)
        return parsed.path.strip("/").split("/")[-1]

    def create_user(self, first_name: str, last_name: str, username: str, email: str, password: str) -> None:
        self.page.goto(f"{self.gitlab_domain}/users/sign_up", wait_until="networkidle")

        self.page.fill("#new_user_first_name", first_name)
        self.page.fill("#new_user_last_name", last_name)
        self.page.fill("#new_user_username", username)
        self.page.fill("#new_user_email", email)
        self.page.fill("#new_user_password", password)

        try:
            with self.page.expect_navigation():
                self.page.click('[data-qa-selector="new_user_register_button"]')
            assert urlparse(self.page.url).path.strip("/") == "users/sign_up/welcome"
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Failed to register user '{username}': navigation to welcome page timed out.")
        except AssertionError:
            error_container = self.page.locator("div#error_explanation")
            if error_container.count() > 0:
                errors = error_container.locator("ul li").all_inner_texts()
                raise WebArenaEditorException(f"Failed to register user '{username}': {'; '.join(errors)}")
            raise WebArenaEditorException(f"Failed to register user '{username}'; ended up at {self.page.url}.")

        self.page.select_option("#user_role", value="software_developer")
        with self.page.expect_navigation():
            self.page.click('input[type="submit"][value="Get started!"]')
        if self.page.url == self.gitlab_domain:
            raise WebArenaEditorException(f"Failed to register user '{username}' at Welcome screen.")

    def add_user_to_group_as_maintainer(self, group_to_add_to: str, user_to_add: str, timeout_after_actions: int = 2000) -> None:
        self.page.goto(f"{self.gitlab_domain}/groups/{group_to_add_to}/-/group_members", wait_until="networkidle")

        def _existing_members():
            accounts = []
            for row in self.page.query_selector_all("table[data-testid='members-table'] tbody tr"):
                account_cell = row.query_selector("td:nth-child(1)")
                if account_cell:
                    username_element = account_cell.query_selector(".gl-avatar-labeled-sublabel")
                    username = username_element.inner_text().strip() if username_element else ""
                    accounts.append(username.strip("@"))
            return accounts

        if user_to_add in _existing_members():
            print(f"User {user_to_add} was already a member of {group_to_add_to}")
            return

        self.page.click('button:has-text("Invite members")')
        self.page.wait_for_selector("#invite-members-modal-2___BV_modal_content_", state="visible")

        self.page.click("input#invite-members-modal-2_search")
        self.page.fill("input#invite-members-modal-2_search", user_to_add)
        self.page.wait_for_timeout(timeout_after_actions)

        self.page.locator(
            f"ul.dropdown-menu li:has(span.gl-avatar-labeled-sublabel:text-is('{user_to_add}')) button"
        ).click()

        self.page.select_option("#invite-members-modal-2_dropdown", "40")
        self.page.wait_for_timeout(timeout_after_actions)
        self.page.click('#invite-members-modal-2___BV_modal_content_ button:has-text("Invite")')

        self.page.wait_for_selector(
            "div.gl-alert-body:has-text('Members were successfully added')", state="visible"
        )

        if user_to_add not in _existing_members():
            raise WebArenaEditorException(f"Failed to find {user_to_add} as a member after addition")


    def toggle_private_profile(self, desired_state: bool) -> None:
        self.page.goto(f"{self.gitlab_domain}/-/profile")
        checkbox_selector = "label[for='user_private_profile']"
        current_state = self.page.locator(checkbox_selector).is_checked()

        if current_state == desired_state:
            return

        if desired_state:
            self.page.locator(checkbox_selector).check()
        else:
            self.page.locator(checkbox_selector).uncheck()
        self.page.locator('button:has-text("Update profile settings")').click()

        try:
            self.page.wait_for_selector(
                "div.gl-alert.gl-alert-info[data-testid='alert-info'] div.gl-alert-body:text('Profile was successfully updated')",
                state="visible",
            )
            assert self.page.locator(checkbox_selector).is_checked() == desired_state
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Unable to set private profile checkbox to {desired_state}; no success message.")
        except AssertionError:
            raise WebArenaEditorException(f"Unable to set private profile checkbox to {desired_state}; checkbox unchanged.")

    def delete_account(self, password: str) -> None:
        self.page.goto(f"{self.gitlab_domain}/-/profile/account", wait_until="networkidle")

        delete_button_selector = 'button:has-text("Delete account")'
        if self.page.locator(delete_button_selector).count() < 1:
            raise WebArenaEditorException("Failed to delete account: missing Delete account button.")

        self.page.click(delete_button_selector)
        password_field_selector = '[data-qa-selector="password_confirmation_field"]'
        self.page.wait_for_selector(password_field_selector, timeout=2000)
        self.page.locator(password_field_selector).fill(password)

        try:
            with self.page.expect_navigation():
                self.page.locator('[data-qa-selector="confirm_delete_account_button"]').click()
        except PlaywrightTimeoutError:
            raise WebArenaEditorException("Failed to delete account due to timeout.")

        if self.page.url.strip("/") != f"{self.gitlab_domain}/users/sign_in":
            raise WebArenaEditorException(f"Failed to delete account; ended up at {self.page.url}.")


    def _maybe_raise_page_not_found(self, url_of_action: str) -> None:
        container = "div.container h3"
        if self.page.locator(container).count() > 0 and self.page.locator(container).text_content().strip() == "Page Not Found":
            raise WebArenaEditorException(f"Currently logged in user cannot access the page at {url_of_action}")

    def _delete_project_or_group(
        self,
        url_of_action: str,
        confirmation_phrase: str,
        deletion_modal_trigger_button_text: str,
        deletion_confirmation_button_text: str,
        id_of_collapsed_section_with_delete_button: str,
        url_after_success: str,
    ) -> None:
        self.page.goto(url_of_action, wait_until="networkidle")
        try:
            self._maybe_raise_page_not_found(url_of_action)
        except WebArenaEditorException:
            print(f"{url_of_action} not found -- probably already deleted.")
            return

        self.page.click(f'section#{id_of_collapsed_section_with_delete_button} button:has-text("Expand")')

        delete_button_selector = f'button:has-text("{deletion_modal_trigger_button_text}")'
        try:
            self.page.wait_for_selector(delete_button_selector, timeout=1000)
        except PlaywrightTimeoutError:
            raise WebArenaEditorException("Currently logged in user cannot access the deletion button")

        self.page.locator(delete_button_selector).click()
        confirm_input_selector = "input#confirm_name_input"
        self.page.wait_for_selector(confirm_input_selector, timeout=1000)
        self.page.locator(confirm_input_selector).fill(confirmation_phrase)

        try:
            with self.page.expect_navigation():
                self.page.locator(f'button:has-text("{deletion_confirmation_button_text}")').click()
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Failed to delete '{confirmation_phrase}' due to timeout.")

        if self.page.url == url_after_success:
            confirmation_text = self.page.locator("div.gl-alert-body").text_content().strip()
            if not confirmation_text.endswith("is in the process of being deleted."):
                raise WebArenaEditorException(
                    f"Failed to delete '{confirmation_phrase}'; unexpected confirmation text: {confirmation_text}"
                )

    def delete_project(self, namespace_name: str, project_name: str) -> None:
        self._delete_project_or_group(
            url_of_action=f"{self.gitlab_domain}/{namespace_name}/{project_name}/edit",
            confirmation_phrase=f"{namespace_name}/{project_name}",
            deletion_modal_trigger_button_text="Delete project",
            deletion_confirmation_button_text="Yes, delete project",
            url_after_success=f"{self.gitlab_domain}/dashboard/projects",
            id_of_collapsed_section_with_delete_button="js-project-advanced-settings",
        )

    def delete_group(self, namespace_name: str) -> None:
        self._delete_project_or_group(
            url_of_action=f"{self.gitlab_domain}/groups/{namespace_name}/-/edit",
            confirmation_phrase=namespace_name,
            deletion_modal_trigger_button_text="Remove group",
            deletion_confirmation_button_text="Confirm",
            url_after_success=self.gitlab_domain,
            id_of_collapsed_section_with_delete_button="js-advanced-settings",
        )


    def _wait_for_selector(self, selector: str, timeout: int = 1000) -> None:
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
        except PlaywrightTimeoutError:
            raise WebArenaEditorException(f"Timed out waiting for selector {selector} on {self.page.url}")

    def replace_file_with_local_file(self, namespace_name: str, project_name: str, branch_name: str, file_name: str, local_file_path: str) -> None:
        self.page.goto(
            f"{self.gitlab_domain}/{namespace_name}/{project_name}/-/blob/{branch_name}/{file_name}",
            wait_until="networkidle",
        )

        self.page.click('[data-testid="replace"]')
        self._wait_for_selector("#modal-replace-blob___BV_modal_body_")
        self.page.locator('input[name="upload_file"]').set_input_files(local_file_path)

        confirm_selector = "#modal-replace-blob___BV_modal_footer_ > button.btn.js-modal-action-primary.btn-confirm.btn-md.gl-button"
        self._wait_for_selector(confirm_selector)
        self.page.click(confirm_selector)

        self.page.wait_for_selector(
            "div.gl-alert-body:has-text('Your changes have been successfully committed')", state="visible"
        )


    def _best_effort_delete(self, url_of_action: str, btn_delete_selector: str, confirm_delete_button: str, kind: str, expand_selector: str = None) -> None:
        self.page.goto(url_of_action, wait_until="networkidle")
        try:
            if expand_selector:
                self._wait_for_selector(expand_selector, timeout=3000)
                self.page.click(expand_selector)
                self.page.wait_for_timeout(1000)
            self.page.wait_for_selector(btn_delete_selector, timeout=3000)
            self.page.query_selector(btn_delete_selector).click()
            self.page.wait_for_timeout(1000)
            self._wait_for_selector(confirm_delete_button, timeout=3000)
            self.page.click(confirm_delete_button)
            self.page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[Warning] Could not delete {kind} at {url_of_action} (may already be deleted): {e}")

    def delete_deploy_key(self, namespace_name: str, project_name: str) -> None:
        self._best_effort_delete(
            url_of_action=f"{self.gitlab_domain}/{namespace_name}/{project_name}/-/deploy_keys",
            btn_delete_selector="#js-deploy-keys-settings > div.settings-content > div > div.deploy-keys-panel.table-holder > div.gl-responsive-table-row.deploy-key > div.table-section.section-15.table-button-footer.deploy-key-actions > div > button",
            confirm_delete_button="#confirm-remove-deploy-key___BV_modal_footer_ > button.btn.js-modal-action-primary.btn-danger.btn-md.gl-button",
            kind="deploy key",
        )

    def delete_deploy_token(self, namespace_name: str, project_name: str) -> None:
        self._best_effort_delete(
            url_of_action=f"{self.gitlab_domain}/{namespace_name}/{project_name}/-/settings/repository#js-deploy-tokens",
            btn_delete_selector="#js-deploy-tokens > div.settings-content > div.table-responsive.deploy-tokens > table > tbody > tr > td:nth-child(6) > div > button",
            confirm_delete_button="#revoke-modal-1___BV_modal_footer_ > a",
            kind="deploy token",
            expand_selector="#js-deploy-tokens > div.settings-header > button",
        )

    def delete_webhook(self, namespace_name: str, project_name: str) -> None:
        url_of_action = f"{self.gitlab_domain}/{namespace_name}/{project_name}/-/hooks"
        btn_delete_selector = "a >> text=Delete"
        confirm_delete_button = "#confirmationModal___BV_modal_footer_ > button.btn.js-modal-action-primary.btn-danger.btn-md.gl-button"
        self.page.goto(url_of_action, wait_until="networkidle")
        while True:
            try:
                self.page.wait_for_selector(btn_delete_selector, timeout=3000)
                self.page.query_selector(btn_delete_selector).click()
                self.page.wait_for_timeout(1000)
                self._wait_for_selector(confirm_delete_button, timeout=3000)
                self.page.click(confirm_delete_button)
                self.page.wait_for_timeout(1000)
            except Exception as e:
                print(f"[Warning] No more webhooks to delete at {url_of_action} (or error): {e}")
                break

    def delete_ssh_key(self) -> None:
        self._best_effort_delete(
            url_of_action=f"{self.gitlab_domain}/-/profile/keys",
            btn_delete_selector="#content-body > div.row.gl-mt-3.js-search-settings-section > div.col-lg-8 > div.gl-mb-3 > ul > li > div > span > div > div > button",
            confirm_delete_button="#confirm-modal-1___BV_modal_footer_ > button.btn.btn-danger",
            kind="SSH key",
        )

    def delete_access_token(self) -> None:
        url_of_action = f"{self.gitlab_domain}/-/profile/personal_access_tokens"
        btn_delete_selector = "a[aria-label='Revoke']"
        confirm_delete_button = "#confirmationModal___BV_modal_footer_ > button.btn.js-modal-action-primary.btn-danger.btn-md.gl-button"
        self.page.goto(url_of_action, wait_until="networkidle")
        while True:
            try:
                self.page.wait_for_selector(btn_delete_selector, timeout=3000)
                self.page.query_selector(btn_delete_selector).click()
                self.page.wait_for_timeout(1000)
                self._wait_for_selector(confirm_delete_button, timeout=3000)
                self.page.click(confirm_delete_button)
                self.page.wait_for_timeout(1000)
            except Exception as e:
                print(f"[Warning] No more access tokens to delete at {url_of_action} (or error): {e}")
                break
