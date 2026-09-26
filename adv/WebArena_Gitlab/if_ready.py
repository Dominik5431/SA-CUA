import requests
import time
import logging
logger = logging.getLogger("adv.WebArena_Gitlab.if_ready")


def check_server_ready_gitlab(url, interval=20, max_retries=40, port=8023, jump_wait=None):

    logger.info(f"Checking the status of GitLab on the url: {url}")
    url = f"http://{url}:{port}/users/sign_in"
    retry_count = 0
    while retry_count < max_retries:
        try:
            logger.info(f"Try #{retry_count + 1} times: connecting to {url}")
            response = requests.get(url, timeout=10)

            if response.status_code == 200 and "Internal Server Error" not in response.text:
                logger.info(f"GitLab on the url {url} is ready.")
                if jump_wait:
                    return True
                time.sleep(60)
                return True

            logger.info(f"Not ready yet (status {response.status_code})")
        except requests.exceptions.RequestException as e:
            logger.info(f"Retry...")

        retry_count += 1
        if retry_count < max_retries:
            logger.info(f"Wait for {interval} and retry...")
            time.sleep(interval)

    logger.error(f"Max tries: ({max_retries}) is reached. GitLab on the url {url} is not ready.")
    return False
