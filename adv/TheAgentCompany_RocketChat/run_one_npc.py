import asyncio
from server import run_server
import argparse
import logging
import os
import json

from npc_model import resolve_npc_model

FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
logging.basicConfig(
    level=15,
    format=FORMAT,
    datefmt="[%X]",
    handlers=[
        logging.StreamHandler(),
    ],
) 

NPC_MODEL = resolve_npc_model()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent_name', type=str, help="Input NPC first name")
    parser.add_argument('--scenarios_json_data', type=json.loads, help="scenarios_json_data")
    parser.add_argument('--instance_suffix', default='', help="RocketChat instance suffix for multi-instance runs (e.g. '-2')")

    args = parser.parse_args()

    asyncio.run(
        run_server(
            model_dict={
                "env": NPC_MODEL,
                "agent1": "rocketchat",
                "agent2": NPC_MODEL,
            },
            agents_roles={
                "agent1": "",
                "agent2": "",
            },
            agent_name = args.agent_name,
            scenarios_json_data = args.scenarios_json_data
        )
    )

if __name__ == "__main__":
    main()