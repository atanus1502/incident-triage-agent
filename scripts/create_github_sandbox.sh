#!/usr/bin/env bash
# Creates a PRIVATE synthetic GitHub repo for testing the triage agent. Usage: create_github_sandbox.sh <owner>/<repo>
# Contains made-up code only. Delete it any time with GitHub's repo settings page.
set -euo pipefail

OWNER=${1:?usage: create_github_sandbox.sh <owner>/<repo>}
WORK=$(mktemp -d)
cd "$WORK"
TRAILER=$'\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'

deploy() {  # deploy <sha> <description>
  local id
  id=$(gh api -X POST "repos/$OWNER/deployments" --jq .id --input - <<EOF
{"ref":"$1","environment":"production","description":"$2","auto_merge":false,"required_contexts":[]}
EOF
)
  gh api -X POST "repos/$OWNER/deployments/$id/statuses" -f state=success >/dev/null
}

git init -q -b main
mkdir -p src/transfers docs

cat > README.md <<'EOF'
# payments-api-sandbox

Synthetic test repo for the incident triage agent. Not a real service; contains no real data.
EOF
cat > requirements.txt <<'EOF'
sqlalchemy==2.0.36
fraud-scoring-client==4.2.0
EOF
cat > docs/openapi.yaml <<'EOF'
openapi: 3.0.3
info:
  title: Payments API
  description: Payments APIi for transfers
  version: "2026.1"
EOF
cat > src/db.py <<'EOF'
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine("postgresql://payments@postgres-payments/payments", pool_size=40, max_overflow=10, pool_timeout=5)
db = sessionmaker(bind=engine)()
EOF
cat > src/transfers/validation.py <<'EOF'
class InvalidTransfer(Exception):
    pass


def validate_transfer(req):
    if req.total_amount <= 0:
        raise InvalidTransfer("amount must be positive")
    if not req.payees:
        raise InvalidTransfer("at least one payee is required")
    return True
EOF
git add -A
git commit -q -m "Initial payments API service$TRAILER"
gh repo create "$OWNER" --private --description "Synthetic test repo for the incident triage agent" --source . --push >/dev/null
deploy "$(git rev-parse HEAD)" "Release 2026.1-1"

sed -i '' 's/Payments APIi/Payments API/' docs/openapi.yaml
git commit -qam "Fix typo in OpenAPI description$TRAILER"
sed -i '' 's/fraud-scoring-client==4.2.0/fraud-scoring-client==4.2.1/' requirements.txt
git commit -qam "Bump fraud-scoring client to 4.2.1$TRAILER"
git push -q origin main

git checkout -q -b per-payee-transfer-limits
cat > src/transfers/validation.py <<'EOF'
from src.db import db
from src.models import TransferLimit


class InvalidTransfer(Exception):
    pass


class LimitExceeded(InvalidTransfer):
    pass


def validate_transfer(req):
    if req.total_amount <= 0:
        raise InvalidTransfer("amount must be positive")
    if not req.payees:
        raise InvalidTransfer("at least one payee is required")
    for payee in req.payees:
        limit = db.query(TransferLimit).filter_by(payee_id=payee.id).first()
        if limit and payee.amount > limit.max_amount:
            raise LimitExceeded(payee.id)
    return True
EOF
git commit -qam "Check per-payee transfer limits during transfer validation$TRAILER"
git push -q -u origin per-payee-transfer-limits
PR=$(gh pr create --base main --head per-payee-transfer-limits \
  --title "Check per-payee transfer limits during transfer validation" \
  --body "$(printf 'Enforces the per-payee limit from the transfer_limits table when validating a transfer.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)')")
gh pr merge "$PR" --merge --delete-branch >/dev/null
git checkout -q main
git pull -q origin main
deploy "$(git rev-parse HEAD)" "Release 2026.1-2"

echo "Private: $(gh repo view "$OWNER" --json isPrivate --jq .isPrivate)"
gh api "repos/$OWNER/deployments" --jq '.[] | "\(.created_at) \(.sha[0:12]) \(.description)"'
echo "Done: https://github.com/$OWNER"
