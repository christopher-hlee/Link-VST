# Moving this into its own repository

Crate Digger is a standalone project. It is parked here only because the
session that built it could not create a new GitHub repo (the integration is
not granted repo-creation scope). Nothing in it depends on LinkVST.

To give it its own home:

```bash
# 1. Create an empty repo at https://github.com/new named `crate-digger`
#    (no README, no .gitignore, no licence — leave it bare).

# 2. From a clone of Link-VST, on this branch:
cd crate-digger
rm EXTRACTING.md
git init -b main
git add -A
git commit -m "Crate Digger: a local-first sample crate-digging studio"
git remote add origin git@github.com:christopher-hlee/crate-digger.git
git push -u origin main

# 3. Then drop it from this branch:
cd .. && git rm -r --cached crate-digger && git commit -m "Move Crate Digger to its own repo"
```

If you would rather keep the original commit history, use `git subtree split`
from the Link-VST root instead of step 2:

```bash
git subtree split --prefix=crate-digger -b crate-digger-only
git push git@github.com:christopher-hlee/crate-digger.git crate-digger-only:main
```

Then run it:

```bash
cd crate-digger
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/crate serve       # → http://127.0.0.1:8770
```
