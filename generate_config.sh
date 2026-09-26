### normal config generation
# One index per domain (the examples/ subdirectory), not per platform: the WASP
# ports carry "domain": "wasp_reddit"/"wasp_gitlab" on their raw goals and land
# in their own suite directory.  See domain_of() in generate_config.py.
rm -f evaluation_examples/test_all_owncloud.json
rm -f evaluation_examples/test_all_reddit.json
rm -f evaluation_examples/test_all_rocketchat.json
rm -f evaluation_examples/test_all_wasp_reddit.json
rm -f evaluation_examples/test_all_wasp_gitlab.json

python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_setup_project.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_setup_project.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_linux_tutorial.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_linux_tutorial.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_docker_entry.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_docker_entry.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_gitclone.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_gitclone.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_alias_tmux.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_alias_tmux.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_install_termcolor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_install_termcolor.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_git_config.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_git_config.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_install_nodejs.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_install_nodejs.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_vim_editor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_vim_editor.json" --output_path "evaluation_examples"


### WASP scenarios (facebookresearch/wasp, ported via convert_wasp_examples.py). These will
### generate configs with unresolved __SEED_ISSUE_IID__ / __SEED_POST_ID__ placeholders until
### the one-time GitLab issue + Reddit post seeding step has run and patched the goals/*.json
### source files with real IDs -- see the WASP integration plan.
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_a_security_violating_change_to_a_repository_a_command_to_download_malware_to_zshrc_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_a_security_violating_change_to_a_repository_a_command_to_download_malware_to_zshrc_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_a_security_violating_change_to_a_repository_a_command_to_download_malware_to_zshrc_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_a_security_violating_change_to_a_repository_a_command_to_download_malware_to_zshrc_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_an_ssh_key_to_the_user_account_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_an_ssh_key_to_the_user_account_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_an_ssh_key_to_the_user_account_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_an_ssh_key_to_the_user_account_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_member_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_member_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_add_member_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_add_member_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_deploy_key_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_deploy_key_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_deploy_key_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_deploy_key_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_deploy_token_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_deploy_token_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_deploy_token_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_deploy_token_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_personal_access_token_for_the_user_s_account_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_personal_access_token_for_the_user_s_account_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_personal_access_token_for_the_user_s_account_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_personal_access_token_for_the_user_s_account_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_webhook_to_attacker_s_server_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_webhook_to_attacker_s_server_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_create_a_webhook_to_attacker_s_server_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_create_a_webhook_to_attacker_s_server_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_delete_project_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_delete_project_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_delete_project_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_delete_project_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_export_project_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_export_project_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_export_project_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_export_project_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_extract_list_of_contributors_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_extract_list_of_contributors_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_extract_list_of_contributors_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_extract_list_of_contributors_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_make_the_profile_public_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_make_the_profile_public_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_make_the_profile_public_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_make_the_profile_public_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_transfer_project_to_a_new_namespace_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_transfer_project_to_a_new_namespace_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_gitlab_transfer_project_to_a_new_namespace_reaction.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_gitlab_transfer_project_to_a_new_namespace_reaction.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_adding_custom_email_for_password_reset_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_adding_custom_email_for_password_reset_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_adding_custom_email_for_password_reset_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_adding_custom_email_for_password_reset_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_create_a_harmful_comment_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_create_a_harmful_comment_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_create_a_harmful_comment_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_create_a_harmful_comment_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_delete_user_created_post_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_delete_user_created_post_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_delete_user_created_post_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_delete_user_created_post_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_downvote_a_post_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_downvote_a_post_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_downvote_a_post_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_downvote_a_post_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_exfiltrate_information_about_block_listed_users_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_exfiltrate_information_about_block_listed_users_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_exfiltrate_information_about_block_listed_users_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_exfiltrate_information_about_block_listed_users_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_exfiltrate_the_private_messages_of_the_user_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_exfiltrate_the_private_messages_of_the_user_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_exfiltrate_the_private_messages_of_the_user_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_exfiltrate_the_private_messages_of_the_user_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_make_a_post_pushing_a_political_slogan_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_make_a_post_pushing_a_political_slogan_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_make_a_post_pushing_a_political_slogan_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_make_a_post_pushing_a_political_slogan_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_negative_comment_on_a_post_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_negative_comment_on_a_post_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_negative_comment_on_a_post_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_negative_comment_on_a_post_upvote.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_phishing_through_private_messages_comment.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_phishing_through_private_messages_comment.json" --output_path "evaluation_examples"
python generate_config.py --benign_task_raw "goals/benign/benign_task.raw_own_wasp_reddit_phishing_through_private_messages_upvote.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_wasp_reddit_phishing_through_private_messages_upvote.json" --output_path "evaluation_examples"



### config generation for end2end task
# rm -f evaluation_examples/test_all_owncloud_for_end2end.json
# rm -f evaluation_examples/test_all_reddit_for_end2end.json
# rm -f evaluation_examples/test_all_rocketchat_for_end2end.json

# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_setup_project.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_setup_project.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_linux_tutorial.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_linux_tutorial.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_docker_entry.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_docker_entry.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_gitclone.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_gitclone.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_alias_tmux.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_alias_tmux.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_install_termcolor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_install_termcolor.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_git_config.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_git_config.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_install_nodejs.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_install_nodejs.json" --output_path "evaluation_examples"
# python generate_config_for_end2end_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_vim_editor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_vim_editor.json" --output_path "evaluation_examples"



### config generation for pointer only task
# rm -f evaluation_examples/test_all_owncloud_for_pointer.json
# rm -f evaluation_examples/test_all_reddit_for_pointer.json
# rm -f evaluation_examples/test_all_rocketchat_for_pointer.json

# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_setup_project.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_setup_project.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_linux_tutorial.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_linux_tutorial.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_docker_entry.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_docker_entry.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_gitclone.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_gitclone.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_alias_tmux.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_alias_tmux.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_install_termcolor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_install_termcolor.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_git_config.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_git_config.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_install_nodejs.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_install_nodejs.json" --output_path "evaluation_examples"
# python generate_config_for_pointer_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_vim_editor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_vim_editor.json" --output_path "evaluation_examples"



### config generation for defense task
# rm -f evaluation_examples/test_all_owncloud_for_defense.json
# rm -f evaluation_examples/test_all_reddit_for_defense.json
# rm -f evaluation_examples/test_all_rocketchat_for_defense.json

# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_setup_project.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_setup_project.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_linux_tutorial.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_linux_tutorial.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_owncloud_docker_entry.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_owncloud_docker_entry.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_gitclone.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_gitclone.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_alias_tmux.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_alias_tmux.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_reddit_install_termcolor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_reddit_install_termcolor.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_git_config.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_git_config.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_install_nodejs.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_install_nodejs.json" --output_path "evaluation_examples"
# python generate_config_for_defense_test.py --benign_task_raw "goals/benign/benign_task.raw_own_rocketchat_vim_editor.json" --adversary_task_raw "goals/adv/adversary_task.raw_own_rocketchat_vim_editor.json" --output_path "evaluation_examples"


### derived output
# The suite layout and the union indexes (test_all_rtc.json, test_all_wasp.json,
# test_all_rtc_extended.json).  Idempotent -- the moves are no-ops once the raw
# goals carry their "domain", it just rebuilds the indexes from what is on disk.
bash migrate_examples_layout.sh

# The phrase-free twins: examples/<suite>_fixed/ plus test_all_*_fixed.json.
# Rebuilt from scratch, so it has to run after the configs above are final.
bash make_rtc_fixed.sh

# The effort-ratio suite: examples/wasp_effort_{gitlab,reddit}/ plus their indexes.
# 6 attack classes x 3 injected-action cost tiers x 2 injection formats = 36 cases, with the
# benign task held fixed at the shape the 168-case suite already uses (the agent sees the
# injected field but never has to touch it). Self-contained -- its scenario table lives in
# the script rather than in goals/, so it is rebuilt from scratch and cannot drift.
python evaluation_examples/generate_wasp_effort_examples.py --output_path evaluation_examples

# Reddit evaluator soundness: rewrites the 72 wasp_reddit configs AND their goals/ sources
# onto episode-scoped reddit_state ground truth. Runs last so a regenerated config cannot
# revert it; idempotent, and a no-op once the goals carry the fix.
python fix_wasp_reddit_eval.py
