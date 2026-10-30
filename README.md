# ai-software-factory

Turns business requirements into working code against factory-sample-app. Two stages:

**1. Plan** — an architect agent reads a Jira epic (the business requirements) and
drafts a design plus a task breakdown. Nothing is created in Jira yet — review it first.

```
python plan.py KAN-5          # KAN-5 is the epic
```

Writes `plan-KAN-5.md` (read this) and `plan-KAN-5.json` (edit this if you want changes).

**2. Create tasks** — once you're happy with the plan, create the tasks in Jira
under the epic:

```
python create_tasks.py plan-KAN-5.json
```

**3. Build** — a developer agent picks up one task at a time, writes the code
against factory-sample-app, runs its tests, and opens a PR:

```
python main.py KAN-9          # KAN-9 is one of the tasks create_tasks.py made
```

The developer agent isn't tied to any particular language or framework — it reads
the approved design notes plus whatever's already in the repo and works from there,
so the same loop can build a different app for a different epic later.

Phase 1: developer only, no tester/reviewer agent yet — that comes once this loop
proves out.

## Setup

```
pip install -r requirements.txt
cp env.sample .env   # fill in JIRA_*, GITHUB_TOKEN, LITELLM_KEY
```
