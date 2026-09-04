# Cloning a private repo onto a headless Pi

How this repository was cloned onto a Raspberry Pi over SSH, why the
obvious approach fails, and the three ways to make it work.

The problem in one line: **a private GitHub repo needs credentials, and
a headless Pi has no browser to log in with and no human at the keyboard
to type a password into.**

Everything below is what actually happened cloning `WalkieTalkie` onto
`meng@192.168.0.83`, including the errors, so the symptoms are real ones
you can match against.

---

## Start by identifying which wall you hit

Run these two probes on the Pi first. They take five seconds and tell
you exactly which of the methods below you need.

```bash
ssh meng@192.168.0.83

# 1. Is the repo public? (does HTTPS work without credentials?)
GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/YOU/REPO.git

# 2. Does this Pi already have a key GitHub accepts?
ssh -T git@github.com
```

`GIT_TERMINAL_PROMPT=0` matters. Without it, git blocks forever waiting
for a username that no one will ever type, and a scripted SSH session
hangs until it times out.

| What you see | What it means | Go to |
|---|---|---|
| `ls-remote` prints refs | repo is public | just `git clone`, done |
| `could not read Username for 'https://github.com'` | repo is private, no credentials | pick a method below |
| `Hi YOU! You've successfully authenticated` | the Pi's key already works | `git clone git@github.com:YOU/REPO.git` |
| `git@github.com: Permission denied (publickey)` | the Pi has no authorised key | pick a method below |

Both of the failure lines are what this Pi produced.

---

## Pick a method

| | Key stored on the Pi | Scope of access | Survives reboot | Best for |
|---|---|---|---|---|
| **A. Agent forwarding** | none | your whole account, only while connected | no | a one-off clone; an untrusted or shared device |
| **B. Deploy key** | one key, this repo only | one repository, read-only by default | yes | a permanent device that pulls updates |
| **C. Account key** | one key, full account | every repo you can reach | yes | your own workstation-like device |

**Use A to get the code there now, then add B if the device needs to
pull updates on its own.** That is the combination used here.

Method D — HTTPS with a personal access token — is deliberately left to
last, because it writes a password-equivalent secret into a file on the
device. See the end.

---

## Method A — SSH agent forwarding (no key on the Pi)

Your workstation already has a key GitHub accepts. Forwarding lets the
Pi *borrow* it for the length of the connection. Nothing is written to
the Pi, and access ends when you disconnect.

### 1. Confirm your workstation has a loaded agent

```bash
ssh-add -l
```

```
256 SHA256:WgvWo9qEFMMTZqWojX0cOXEsp1JIyycFQTi8Fx6fyDg mengkungkao@gmail.com (ED25519)
```

`The agent has no identities` means nothing is loaded — add your key
first, or agent forwarding will forward an empty agent:

```bash
ssh-add ~/.ssh/id_ed25519_github
```

To be sure it is the key GitHub honours, check which one gets accepted:

```bash
ssh -v -T git@github.com 2>&1 | grep -E "Offering public key|Authenticated to"
```

```
debug1: Offering public key: /home/meng/.ssh/id_ed25519_github ED25519 SHA256:WgvWo9qE... explicit agent
Authenticated to github.com ([4.237.22.38]:22) using "publickey".
```

### 2. Connect with `-A` and clone

```bash
ssh -A meng@192.168.0.83

# on the Pi -- prove the agent came across
ssh-add -l                    # should list your workstation's keys
ssh -T git@github.com         # should greet you by name

git clone git@github.com:Mengkungkao/WalkieTalkie.git
```

The URL **must** be the SSH form `git@github.com:OWNER/REPO.git`. A
forwarded agent does nothing for an `https://` URL — git will still ask
for a username.

### 3. One-liner version

```bash
ssh -A meng@192.168.0.83 'git clone git@github.com:Mengkungkao/WalkieTalkie.git'
```

### Security note

Agent forwarding lets anyone with **root on that Pi** use your agent
socket to authenticate as you, for as long as you stay connected. That
is fine for a Pi you own on your own LAN. Do not forward into machines
you do not control. `ssh -A` is a deliberate, per-connection choice —
never put `ForwardAgent yes` in a global `~/.ssh/config` block.

---

## Method B — Deploy key (the right answer for a permanent device)

A deploy key is an SSH key authorised for **one repository**, read-only
unless you say otherwise. If the Pi is compromised, the blast radius is
this repo, not your account.

### 1. Generate a key on the Pi

```bash
ssh-keygen -t ed25519 -C "walkie-pi4b" -f ~/.ssh/id_ed25519_walkie -N ""
cat ~/.ssh/id_ed25519_walkie.pub
```

`-N ""` leaves it without a passphrase, which is what you want on an
unattended device: a passphrase-protected key needs a human at boot.
The read-only scope is what keeps that acceptable.

### 2. Add it to the repository

GitHub → your repo → **Settings** → **Deploy keys** → **Add deploy key**
→ paste the `.pub` line → leave *Allow write access* unchecked unless
the Pi genuinely needs to push.

### 3. Tell SSH to use it for GitHub

```bash
cat >> ~/.ssh/config <<'CONF'
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_walkie
    IdentitiesOnly yes
CONF
chmod 600 ~/.ssh/config
```

`IdentitiesOnly yes` is not optional. Without it SSH offers every key it
can find, GitHub rejects after five wrong ones, and you get
`Permission denied (publickey)` while holding a perfectly good key.

### 4. Verify and clone

```bash
ssh -T git@github.com          # "Hi YOU/REPO! You've successfully authenticated"
git clone git@github.com:Mengkungkao/WalkieTalkie.git
```

A deploy key greets you with `Hi OWNER/REPO!`, not `Hi USERNAME!` —
that is how you know the key is repo-scoped rather than account-wide.

---

## Method C — Account key on the Pi

Same as B, but you paste the public key into
**github.com → Settings → SSH and GPG keys → New SSH key** instead of
the repository's deploy keys.

This gives that Pi your full account access for every repo. Only do it
for a device you treat like your own workstation.

Then the same `~/.ssh/config` block and `ssh -T git@github.com` check as
in method B.

---

## Verify the clone actually matches

`git clone` printing no error is not proof. Two checks worth making,
especially over a flaky Wi-Fi link to a Pi Zero.

```bash
cd ~/WalkieTalkie
git log --oneline
git rev-parse HEAD
git status --porcelain | wc -l     # 0 = nothing modified
git ls-files | wc -l
```

Compare the commit against the remote directly, rather than trusting
local state:

```bash
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git ls-remote origin refs/heads/main | cut -f1)
[ "$LOCAL" = "$REMOTE" ] && echo MATCH || echo MISMATCH
```

This is worth doing after a **push**, too. Pushing to a repository that
had no commits printed `Everything up-to-date` here — which reads like
nothing happened. `git ls-remote origin` showed the branch sitting at
the right SHA, confirming the push had in fact landed.

---

## Troubleshooting: the exact errors

### `fatal: could not read Username for 'https://github.com': terminal prompts disabled`

The repo is private and there are no HTTPS credentials. You either set
`GIT_TERMINAL_PROMPT=0` yourself (good — it fails fast instead of
hanging), or git is running somewhere with no terminal.

Fix: use an SSH remote and one of the methods above.

If an existing clone has an HTTPS remote, switch it in place — this
keeps all your commits and history:

```bash
git remote -v                       # origin  https://github.com/...
git remote set-url origin git@github.com:Mengkungkao/WalkieTalkie.git
git remote -v                       # origin  git@github.com:...
```

### `git@github.com: Permission denied (publickey).`

SSH reached GitHub but had no key it would accept. Diagnose with:

```bash
ssh -vT git@github.com 2>&1 | grep -E "Offering|Authenticated|no mutual"
```

- *No `Offering public key` lines at all* — there is no key, or your
  `~/.ssh/config` points at a file that does not exist. Check with
  `ls -l ~/.ssh/`.
- *Several `Offering` lines, all refused* — none of your keys is
  registered. Add one (method B or C), and set `IdentitiesOnly yes` so
  SSH stops burning attempts on the wrong keys.
- *Works locally but not over `ssh -A`* — your agent is empty. Run
  `ssh-add -l` on the **workstation**, not the Pi.

### `Host key verification failed`

First contact with a new host. For a scripted run, accept on first use:

```bash
ssh -o StrictHostKeyChecking=accept-new -T git@github.com
```

Do not use `StrictHostKeyChecking=no` — it silently accepts a changed
key too, which is the case that actually matters.

### Permissions errors on the key

SSH ignores keys and configs that others can read:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519_walkie ~/.ssh/config
chmod 644 ~/.ssh/id_ed25519_walkie.pub
```

---

## Gotchas when driving this over SSH from a script

These cost real time during this clone.

**A `head` in a pipeline can abort the whole script.** With `set -e`,
this kills everything after it:

```bash
set -e
ssh -T git@github.com 2>&1 | head -1     # head exits, SIGPIPE, non-zero status
git clone ...                            # never runs, and says nothing
```

The clone silently never happened. Either drop `set -e`, or do not pipe
into `head`. The symptom is output that just stops partway with no error.

**Use `BatchMode=yes` for anything scripted.** It makes SSH fail
immediately instead of prompting for a password into a script that
cannot answer.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=25 meng@192.168.0.83 '...'
```

**Give a Pi Zero 2 W a generous `ConnectTimeout`.** 10 seconds is not
enough over Wi-Fi and produces intermittent hangs; 25 is reliable.

**Beware `sudo` timestamps expiring mid-script.** `sudo -n true` can
succeed during a check and fail seconds later in the command that
matters, because the cached credential aged out. Test and act in the
*same* `sudo` invocation:

```bash
sudo -n bash -c 'cp a a.bak && sed -i "..." a'
```

---

## Pulling updates afterwards

If you used method A, the Pi cannot pull on its own — the agent went
away with your session. Check with:

```bash
GIT_SSH_COMMAND="ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519" \
  git ls-remote origin >/dev/null 2>&1 && echo "can pull" || echo "cannot pull"
```

Either add a deploy key (method B), or keep forwarding for each pull:

```bash
ssh -A meng@192.168.0.83 'cd ~/WalkieTalkie && git pull'
```

### Pushing from the Pi

Needs a key with write access — a deploy key with *Allow write access*
checked, or an account key. Then set the author identity, or commits
land as `pi@raspberrypi`:

```bash
git config user.name  "Mengkungkao"
git config user.email "mengkungkao@gmail.com"
```

Drop `--global` to keep it to this repo, as above.

---

## Method D — HTTPS with a personal access token

Works, and is the least good option: it stores a password-equivalent
secret in a plaintext file on the device.

```bash
gh auth login                                   # if the gh CLI is available
# or, manually:
git config --global credential.helper store     # writes ~/.git-credentials in the clear
git clone https://github.com/Mengkungkao/WalkieTalkie.git
```

**Never** embed the token in the remote URL
(`https://TOKEN@github.com/...`) — it ends up in `.git/config`, in
`git remote -v` output, and in any log or screenshot of either.

A fine-grained token limited to one repository is the least-bad
variation. An SSH deploy key is better in every respect.

---

## What was done here

```bash
# workstation: confirm the agent holds a key GitHub accepts
ssh-add -l

# clone with the agent forwarded -- no key written to the Pi
ssh -A meng@192.168.0.83 \
  'git clone git@github.com:Mengkungkao/WalkieTalkie.git'

# verify the clone matches the remote
ssh meng@192.168.0.83 'cd WalkieTalkie && git rev-parse HEAD'
git ls-remote origin refs/heads/main | cut -f1
```

Both printed `753e3943bce6521e6786518cda7e543f83bfff92`.

To let that Pi pull on its own later, add its public key as a deploy key
(method B) — it was generated but not yet registered:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIkG93dICJ5EQ/ruXolof7Q6dJGG33tPotP2BSoPdopQ mengkungkao@gmail.com
```
