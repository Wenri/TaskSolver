#!/bin/sh
# hand_over.sh COMMIT BRANCH TARGETS LOG: push an adoption that needs hand work to its branch and
# open the issue that holds its runtimes (or comment on the open one). Run by the workflow with
# GH_TOKEN, GITHUB_RUN_ID, RUN_URL and RUNNER_TEMP set, from the repository root.
set -eu
commit=$1 branch=$2 targets=$3 log=$4
git push --force origin "$commit:refs/heads/$branch"
title=$(python3 tools/runtimes.py title "$targets")
{
    echo "Adopting **$targets** failed in [run $GITHUB_RUN_ID]($RUN_URL)."
    echo "The attempt is on branch \`$branch\`; finish it there and merge, or close this issue to retry."
    echo
    echo "Last lines of \`$(basename "$log")\`:"
    echo '```'
    tail -n 80 "$log"
    echo '```'
} > "$RUNNER_TEMP/issue.md"
number=$(gh issue list --state open --search "\"$title\" in:title" --json number,title \
    --jq "map(select(.title == \"$title\"))[0].number // empty")
if [ -n "$number" ]; then
    gh issue comment "$number" --body-file "$RUNNER_TEMP/issue.md"
else
    gh issue create --title "$title" --body-file "$RUNNER_TEMP/issue.md"
fi
