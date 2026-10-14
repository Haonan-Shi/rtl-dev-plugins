# claude-plugins

Claude Code plugin marketplace for embedded development and debugging.

## Plugins

| Plugin | Description |
|---|---|
| [debug-analyzer](plugins/debug-analyzer) | Flash with mpcli, capture and decode SoC logs with Realtek DebugAnalyzer (Windows) |

## Install

```
/plugin marketplace add Haonan-Shi/claude-plugins
/plugin install debug-analyzer@haonan-plugins
```

For local development, add the checkout directory instead:

```
/plugin marketplace add C:\path\to\claude-plugins
```

## Layout

```
.claude-plugin/marketplace.json      marketplace index
plugins/<name>/.claude-plugin/plugin.json
plugins/<name>/skills/<skill>/SKILL.md
```
