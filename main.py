import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

import jira_client
from graph import build_graph


# runs one ticket end to end: fetches it from jira (plus the parent epic's design
# notes, if it has one), runs the developer graph, prints the PR link
async def run_ticket(ticket_key):
    ticket = jira_client.get_ticket(ticket_key)
    design_context = None
    if ticket["parent_key"]:
        epic = jira_client.get_ticket(ticket["parent_key"])
        design_context = epic["description"]

    graph = build_graph()
    result = await graph.ainvoke({"ticket": ticket, "design_context": design_context})
    print(f"PR opened: {result['pr_url']}")
    if not result["test_passed"]:
        print("warning: tests failed locally, review before merging")


if __name__ == "__main__":  # python main.py KAN-9
    asyncio.run(run_ticket(sys.argv[1]))
