import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import jira_client

PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "KAN")


# reads a plan-<epic>.json (from plan.py, after you've reviewed/edited it), creates
# each task in jira under the epic, and appends the approved design doc onto the
# epic's own description so developer tickets can read it back as context later
def create_tasks(plan_path):
    with open(plan_path) as f:
        plan = json.load(f)

    for task in plan["tasks"]:
        key = jira_client.create_task(PROJECT_KEY, task["title"], task["description"], epic_key=plan["epic_key"])
        print(f"created {key}: {task['title']}")

    jira_client.append_description(plan["epic_key"], f"--- Approved design ---\n\n{plan['design']}")
    print(f"appended design doc to {plan['epic_key']}")


if __name__ == "__main__":  # python create_tasks.py plan-KAN-5.json
    create_tasks(sys.argv[1])
