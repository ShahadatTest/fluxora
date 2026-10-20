# SKLab / Fluxora — submission review checklist

Checked 20 October 2026. **Source and evidence review package; not a submitted entry.**
SKLab and two members are owner-confirmed. Names, affiliations, eligibility evidence
and registration confirmation have not been supplied. Do not put passports, entry forms,
private contact details or credentials into the public repository or this package.

## Official instruction check

The [university announcement](https://ird.oou.cmu.ac.th/2026-shenzhen-international-fintech-competition/)
and its [linked official guidance](https://drive.scmc.cmu.ac.th/s/2jKErkTJpFmcxYF/download)
were checked again on 20 October. The four-page PDF SHA-256 remains
`9005e42629435a1e129a8367c8d3d5e3714b6ea0bbab30713cd6be2f2bdd0c5a`.

The checklist covers technical documentation, source/setup/tests/GitHub, a presentation,
security self-assessment and execution evidence. It caps the final presentation at ten
minutes and video at five. Video is called optional in one item but also appears in
non-on-chain execution evidence; resolve that discrepancy before omitting it. ZIP is
our convenient source/evidence packaging choice, not a claimed mandatory format.

The announced registration/submission deadline is **20 October 2026, 23:59 Beijing
(UTC+8)**, equivalent to **21:59 Dhaka (UTC+6)**. Entrants should verify any organizer
update before sending. No deadline extension, registration, eligibility or acceptance
is inferred from a working website or public GitHub repository.

## Project deliverables

| Item | Current project status | Review location |
| --- | --- | --- |
| Runnable source and tests | Ready for synthetic evaluation; Python 3.10+, no third-party runtime packages | [README](../README.md), `cashflow_copilot/`, `tests/`, `START_FLUXORA.cmd` |
| Local setup, private AI/Telegram configuration | Documented; public hosted settings remain locked | [Local setup](LOCAL-SETUP.md), [model setup](MODEL-SETUP.md), [Telegram](TELEGRAM.md) |
| Technical design and algorithms | Updated to application source 0.26.4 | [Technical documentation](TECHNICAL.md) |
| Security self-assessment | Current threat/permission boundaries, evidence and known risks | [Security assessment](SECURITY.md) |
| Execution evidence | Synthetic tests, fresh live forecast/DRAFT and rendered judge journey | [Latest walkthrough](JUDGE-WALKTHROUGH-2026-10-20.md), [validation](VALIDATION.md) |
| Public repository and demo | Published; publishing these is not competition submission | [GitHub](https://github.com/ShahadatTest/fluxora), [Home](https://fluxora.sklab.cc/), [workspace](https://fluxora.sklab.cc/app) |
| Presentation | **Pending final revision**; previous draft retained and labelled | [Historical presentation files](../presentation/README.md) |
| Demo recording | **Deferred at owner's request**; no recording is claimed | Final app review precedes recording |
| Team/registration/eligibility | **Pending entrant completion/confirmation** | Owner-provided documents, kept outside public source |

The project's Topic E contribution is evidence-linked cash-flow analysis and constrained
preparation of owner-reviewed work. It does not demonstrate a lender marketplace,
underwriting, real bank integration, real payments or independently validated financing
recommendations. Do not describe all of Topic E as fully solved or claim global uniqueness.

## Package boundaries and final entrant handoff

The review ZIP contains committed source, synthetic fixtures, tests, design assets and
documentation/evidence, plus an inventory and hashes. Existing presentation binaries are
historical drafts even where their retained filenames contain `Final`. Runtime databases,
credentials, vaults, cookie jars, caches, environments, build directories and `.git` history
are excluded. The original competition PDF and private registration documents are not copied.

The technical package check does not finish the entry: supply member information, confirm
registration/eligibility and repository access, finalize presentation/video and review the
actual organizer submission instructions. Nothing has been emailed or submitted by the app
or this packaging workflow. No license grant or ownership/third-party-IP clearance is invented;
entrants must review the rights in their own material and any included assets/dependencies.
