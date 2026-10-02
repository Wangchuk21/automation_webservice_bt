# System overview

Internal tool for Bhutan Telecom staff. It is not a public service and nothing
here is meant to face a customer directly: every outward-facing step is an email
an operator chooses to send, and every destructive step is behind a typed
confirmation and an audit record.

```

                            BT staff only
                          (LAN browser)
                                │
        ┌──────────────────────────────▼────────────────────────────────┐
        │              automation_webservice_bt                         │
        │         FastAPI · one uvicorn worker · :8000                  │
        │                                                               │
        │  app.py — 34 endpoints, all under /api/v1                     │
        │                                                               │
        │  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐  │
        │  │ Provisioning    │ │ Domain service  │ │ Billing         │  │
        │  │ create account  │ │ register on     │ │ surrender       │  │
        │  │ IPv6 · SSL      │ │ nic.bt.bt       │ │ suspend         │  │
        │  │ SFTP · DNS      │ │ DNS check       │ │ activate        │  │
        │  └─────────────────┘ └─────────────────┘ └─────────────────┘  │
        │                                                               │
        │  ┌─────────────────────────────────────────────────┐          │
        │  │ provisioners/   cpanel.py · directadmin.py      │          │
        │  │ ssh_client.py (paramiko) · tls_config.py        │          │
        │  └─────────────────────────────────────────────────┘          │
        │                                                               │
        │  activity · domain_service · suspension · surrender           │
        │  notifier · dns_check · ssl_service · nic_client · bscs       │
        └───────────────┬────────────────────────────────┬──────────────┘
                        │                                │
                        ▼                                ▼
      ┌──────────────────────────────────┐                                          ┌──────────────────────────────────┐
      │ thimpchu.druknet.bt              │                                          │ yongnay.druknet.bt               │
      │ cPanel 138.0                     │                                          │ DirectAdmin 1.711                │
      │ 202.144.128.216                  │                                          │ 202.144.128.131                  │
      │                                  │                                          │                                  │
      │ SSH     :2020                    │                                          │ API     :2222                    │
      │ WHM     :2083                    │                                          │ SFTP    :22                      │
      │                                  │                                          │                                  │
      │ AutoSSL installed, with          │                                          │ Let's Encrypt, per-domain        │
      │ Let's Encrypt — but no package   │                                          │ acme_enabled, renewed by         │
      │ carries the AutoSSL feature, so  │                                          │ DirectAdmin                      │
      │ it declines here                 │                                          │                                  │
      └──────────────────────────────────┘                                          └──────────────────────────────────┘

     every one of these is reached over the network, and none is trusted:
     what they report is checked against DNS before a customer is told

     ┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐
     │ nic.bt.bt              │  │ BSCS                   │  │ SMTP                   │  │ Public DNS             │
     │ national domain        │  │ billing, READ-ONLY     │  │ webmail11.bt.bt        │  │ asked two questions:   │
     │ registry               │  │ 10.0.41.149            │  │ :465                   │  │                        │
     │                        │  │ :8581                  │  │                        │  │ does this domain point │
     │ 23 fields, a tick-box  │  │                        │  │ welcome letter,        │  │ at its own server?     │
     │ on the hosting form    │  │ lapsed contracts       │  │ forwarding, correction │  │ is it forwarded?       │
     └────────────────────────┘  └────────────────────────┘  └────────────────────────┘  └────────────────────────┘

  ═══ durable records ═══  JSONL, append only, last line wins ═══

  ./data/domains/services.jsonl    every domain registered, and every email
                                   ever sent to a customer
  ./data/activity.jsonl             cross-cutting "what has been done"
  ./data/surrenders/audit.jsonl     terminations, with their evidence
  ./data/surrenders/                scanned surrender letters
  ./suspension/audit.jsonl          the nightly run's own decisions
  ./suspension/heartbeat.json       every run, whatever the outcome
```

## The scheduled job

```
02:17 Asia/Thimphu   suspender container
  └─ scripts/suspend_expired.py
       ├─ reads BSCS for lapsed contracts          (network)
       ├─ matches names against accounts on BOTH panels
       ├─ writes ./suspension/audit.jsonl     decisions, only when it got some
       └─ writes ./suspension/heartbeat.json  ALWAYS, whatever the outcome
            └── DETECT ONLY. Nothing is suspended automatically.

  The two files answer different questions. The audit log holds decisions, so a
  run that fails before deciding anything correctly writes nothing to it -- which
  used to make "the job did not run" and "the job ran and failed" look identical.
  The heartbeat is written on every invocation, including a crash, with the
  traceback, so the dashboard can say which of the three it is:

      stale         the list is merely old
      THE JOB FAILED  it ran and crashed; the reason is attached
      NOT RUNNING    nothing has tried for 26h+ — the container or host was
                     not up at 02:17
```

One worker, no `--workers`: the async handlers were converted to plain `def` so
FastAPI runs them in its threadpool rather than serialising every request behind
one blocking call.

## Why the same thing is refused twice

The system checks; it does not trust. Every one of these is a server-side
refusal, so nothing depends on a browser behaving itself:

| Refusal | Why |
|---|---|
| Customer **not** emailed unless DNS proves the forwarding | The email asserts their domain is live. It only says so when that is true. |
| Certificate **not** attempted unless the domain points at the server | Each failed Let's Encrypt validation costs one of five per hostname per week, shared by every customer on the box. |
| `observed` (what DNS returned) **never** editable | That is evidence about the public internet. Rewriting it to match what somebody hoped for is the one thing this exists to prevent. |
| Activation **refused** unless the suspension was billing | A payment does not clear an account suspended for abuse, spam or compromise. |
| Recorded email **never** rewritten by a correction | The customer holds both. So the record holds both. |
| Destructive endpoints **refuse to run** with no `API_AUTH_TOKEN` | Destroying a customer's service must never run unauthenticated. |
| `observed` from a live check **reused** for the customer email | It must be the values that were verified, not the ones that were requested. |

## Two kinds of record, deliberately kept apart

`data/activity.jsonl` is a convenience — one cross-cutting feed answering "what
have we done", including refusals, because a refused activation is the answer to
*"who tried to bring back an account suspended for abuse"*.

It is **not** the record. The surrender audit with its evidence, the suspension
run log and the domain-service log remain the formal trails and are never
replaced by it. A test asserts each still exists.

## Layout

| Module | Lines | What it owns |
|---|---|---|
| `app.py` | 1676 | every endpoint, and the post-create step order |
| `provisioners/cpanel.py` | 769 | cPanel: account, IPv6, suspend, activate |
| `provisioners/directadmin.py` | 754 | DirectAdmin: account, SFTP, suspend, activate |
| `nic_client.py` | 686 | nic.bt.bt, its 23 fields and what it will accept |
| `bscs_client.py` | 555 | BSCS, strictly read-only |
| `suspension.py` | 482 | the nightly detection and its refusals |
| `dns_check.py` | 411 | every DNS question, plus what the customer is told |
| `ssl_service.py` | 399 | certificates, gated on DNS |
| `domain_service.py` | 377 | registration → awaiting_dns → verified → notified |
| `surrender.py` | 371 | termination, evidence and ordering |
| `notifier.py` | 336 | every customer-facing email |
| `activity.py` | 133 | the cross-cutting feed |

## Two things that will bite whoever deploys this next

`Dockerfile` copies modules by an **explicit list**, not `COPY . .`. A new module
that is not added there is silently missing from the image, and every account
gets created with no certificate and no error. It has bitten this project twice;
there is a test that checks `ssl_service.py` is listed.

`docker compose up -d` **without `--build`** reuses the old image and keeps the
old code running. It always needs `--build`.

## Known state

- cPanel AutoSSL is installed on thimpchu and already holds a Let's Encrypt
  account. The certificate step still declines, for one reason: a cPanel package
  does not list features itself, it names a *feature list*, and AutoSSL is not
  ticked in the "default" one every package points at. Tick it in WHM under
  Packages, and the step will run. Everything else on cPanel works.
- DirectAdmin's Let's Encrypt works with no licence; it is a per-domain switch,
  enabled before a certificate is requested.
- This only covers **new** accounts. Accounts already on either panel are not
  backfilled.
