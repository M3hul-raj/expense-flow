# Standing rules for agents working on this repo
- Branch per milestone; one commit per change; never amend, force-push, or touch main; never merge or deploy.
- New behavior needs a failing test first, then the fix; show red and green.
- Secrets and real data never go in the repo, prompts, logs or output. Credentials come only from env vars. Real DB copies stay untracked and are never printed.
- Never contact production or my PythonAnywhere account. I do production steps with deploy/pa_deploy.sh.
- Don't change dependencies, workflows, schema, or files outside the approved plan without asking.
- STOP and report when: a failure isn't explained, unexpected files appear in git status, a secret is needed, a step touches production data, or a decision hasn't been approved.
- Keep chat explanations to a few lines; teaching notes go in docs/learning/.
- End each task with git log --oneline, git diff --stat, full test output, and a list of anything unverified.