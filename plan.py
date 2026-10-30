import asyncio
import json
import sys

from dotenv import load_dotenv

load_dotenv()

import architect_agent
import jira_client
import repo_ops


# fetches the epic and the current repo (so the architect designs on top of
# whatever's already built, not in a vacuum), has the architect agent draft a
# design + task list, and saves both a human-readable plan-<epic>.md (read
# this) and a machine-readable plan-<epic>.json (create_tasks.py reads this
# one, after you've reviewed/edited it)
async def plan_epic(epic_key):
    epic = jira_client.get_ticket(epic_key)
    repo_dir = repo_ops.clone_repo()
    repo_files = repo_ops.read_repo_files(repo_dir)
    design, tasks = await architect_agent.generate_plan(epic, repo_files)

    md_path = f"plan-{epic_key}.md"
    with open(md_path, "w") as f:
        f.write(f"# Design for {epic_key}: {epic['summary']}\n\n{design}\n\n## Proposed tasks\n\n")
        for i, task in enumerate(tasks, 1):
            f.write(f"### {i}. {task['title']}\n\n{task['description']}\n\n")

    json_path = f"plan-{epic_key}.json"
    with open(json_path, "w") as f:
        json.dump({"epic_key": epic_key, "design": design, "tasks": tasks}, f, indent=2)

    print(f"wrote {md_path} and {json_path}")
    print(f"review {md_path} (edit {json_path} to match any changes you want), then run:")
    print(f"  python create_tasks.py {json_path}")


if __name__ == "__main__":  # python plan.py KAN-5
    asyncio.run(plan_epic(sys.argv[1]))
