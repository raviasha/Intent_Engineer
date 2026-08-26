# MCP binding examples

These files are credential-free copies of the tested reference bindings. A binding's
`profile_path` is project-relative, so copy both the matching `profiles/mcp/<provider>.yaml` file
and the example binding into a new project:

```bash
export INTENT_ENGINEERING_SOURCE=/path/to/intent-engineering
mkdir -p profiles/mcp .intent/connectors
cp "$INTENT_ENGINEERING_SOURCE/profiles/mcp/slack.yaml" profiles/mcp/slack.yaml
cp "$INTENT_ENGINEERING_SOURCE/examples/mcp-bindings/slack.yaml" .intent/connectors/slack.yaml
```

Adapt the binding to your compatible server and keep credential values outside YAML. `env:NAME` is
resolved only at runtime.
