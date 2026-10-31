import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import architect_agent
import jira_client
import repo_ops

PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "KAN")

# non-interactive combination of plan.py + create_tasks.py -- the orchestrator
# runs this as its own k8s job (see orchestrator.py's _spawn_planning_job) the
# moment it sees an epic with no tasks yet, so a brand new epic (just a title +
# description in jira) turns into real, ready-to-run tasks with no manual step.
# still writes plan-<epic>.md too, purely so there's a record of the design to
# read later -- nothing reads it back programmatically
async def plan_and_create_tasks(epic_key):
    epic = jira_client.get_ticket(epic_key)
    repo_dir = repo_ops.clone_repo()
    repo_files = repo_ops.read_repo_files(repo_dir)
    design, tasks = await architect_agent.generate_plan(epic, repo_files)

    md_path = f"plan-{epic_key}.md"
    with open(md_path, "w") as f:
        f.write(f"# Design for {epic_key}: {epic['summary']}\n\n{design}\n\n## Proposed tasks\n\n")
        for i, task in enumerate(tasks, 1):
            f.write(f"### {i}. {task['title']}\n\n{task['description']}\n\n")

    for task in tasks:
        key = jira_client.create_task(PROJECT_KEY, task["title"], task["description"], epic_key=epic_key)
        print(f"created {key}: {task['title']}")

    jira_client.append_description(epic_key, f"--- Approved design ---\n\n{design}")
    jira_client.add_comment(epic_key, f"Planned automatically -- {len(tasks)} tasks created.")
    print(f"planned {epic_key}: {len(tasks)} tasks created")


if __name__ == "__main__":  # python plan_and_create_tasks.py KAN-31 -- plans the epic and creates its tasks in jira
    asyncio.run(plan_and_create_tasks(sys.argv[1]))
