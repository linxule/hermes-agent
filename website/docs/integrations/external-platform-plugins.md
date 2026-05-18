---
title: "External Platform Plugins"
sidebar_label: "External Platform Plugins"
sidebar_position: 2
---

# External Platform Plugins

A community of external Hermes platform plugins lives outside this repository — separate GitHub repos that ship as drop-in directories for `~/.hermes/plugins/` and exercise the [`ctx.register_platform()`](/docs/developer-guide/adding-platform-adapters#plugin-path-recommended) contract documented in the platform-adapter authoring guide.

This page lists known external platform plugins for **discovery purposes only**. Every entry is **community-maintained** and is **not part of Hermes core support**: Hermes maintainers do not audit, vet, or guarantee compatibility of any listed plugin. Quality, security, ongoing maintenance, and version-compatibility are the responsibility of each plugin's author.

## Known plugins

| Plugin | Platform | Repository |
|---|---|---|
| hermes-napcat | QQ / OneBot 11 | https://github.com/Aliang1337/hermes-napcat |
| hermes-kimi-plugin | Kimi (Moonshot AI) | https://github.com/linxule/hermes-kimi-plugin |
| hermes-plugin-line | LINE (multi-account) | https://github.com/liyoungc/hermes-plugin-line |
| agentgate-hermes | Custom WebSocket bridge | https://github.com/monteslu/agentgate-hermes |
| Hermes-A365 | Microsoft 365 / Bot Framework | https://github.com/satscryption/Hermes-A365 |
| hermes-feishu-message | Feishu / Lark | https://github.com/windinternet/hermes-feishu-message |
| hermes-plugin-webchat | Web Chat (aiohttp + SPA) | https://github.com/XenioxYT/hermes-plugin-webchat |

Entries are ordered alphabetically by repository owner. The seed reflects what was findable via GitHub code search for `ctx.register_platform` at the time this page was created; a wider survey of `hermes-agent` usage suggests more external plugins exist than are listed here.

## Inclusion criteria

A plugin may request listing if **all four** of the following hold:

1. The plugin registers a platform adapter via `ctx.register_platform()` using the documented [Plugin Path](/docs/developer-guide/adding-platform-adapters#plugin-path-recommended) — not a hook plugin, not a tool plugin.
2. The plugin lives in a public GitHub repository with a commit in the last 90 days.
3. The plugin's README documents install steps and `config.yaml` integration.
4. The submission PR notes (in the PR description) the plugin's current deployment status — for example "production use by N deployments," "beta testing with the author's own instance," or "experimental / reference implementation." This is a self-attested signal so readers can calibrate expectations; maintainers do not verify it.

These criteria are mechanical and intentionally low-bar for the initial seed so the list can begin to exist. Criteria may tighten as the surface matures.

## Submitting an entry

To request inclusion, open a PR against this file that adds one row to the table above. The PR should:

- Edit only this file (no other changes).
- Add the entry in alphabetical order by repository owner.
- Include the deployment-status note (criterion 4) in the PR description.

Maintainers can also remove entries that no longer meet the criteria. Pruning PRs are welcome from anyone in the community, not just plugin authors.

## What this list is not

- **Not an endorsement.** Inclusion does not imply that Hermes maintainers have reviewed, audited, or recommend any specific plugin.
- **Not a security review.** Each plugin runs in your Hermes process and inherits its credentials and capabilities. Treat installation as you would any other third-party Python package: read the source, check the issue tracker, and verify the maintainer.
- **Not a compatibility contract.** Plugins may break across Hermes releases. The plugin's author is responsible for declaring and maintaining version support.
- **Not actively curated.** Hermes maintainers do not commit to keeping this list fresh. Entries with broken links or long inactivity may be removed by community PRs at any time; readers should not assume the list is up-to-the-minute accurate.

## Building your own external plugin

If you maintain a Hermes platform plugin that meets the inclusion criteria above, you're welcome to submit it. For background on building a platform plugin, see the [Adding a Platform Adapter](/docs/developer-guide/adding-platform-adapters) guide.
