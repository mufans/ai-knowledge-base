import type { Plugin } from "@opencode-ai/plugin"

const ARTICLES_GLOB = "knowledge/articles/"
const TRIGGER_TOOLS = new Set(["write", "edit"])

const plugin: Plugin = async (input) => {
  const { $, directory } = input

  return {
    "tool.execute.after": async (input, output) => {
      if (!TRIGGER_TOOLS.has(input.tool)) return

      const filePath: string | undefined =
        input.args?.file_path ?? input.args?.filePath
      if (!filePath) return

      if (!filePath.includes(ARTICLES_GLOB)) return
      if (!filePath.endsWith(".json")) return

      try {
        const result = await $`python3 hooks/validate_json.py ${filePath}`
          .cwd(directory)
          .nothrow()
          .quiet()
          .text()

        if (result.trim()) {
          output.output += `\n\n[validate] ${result.trim()}`
        }
      } catch {
        // intentionally swallowed — never block the Agent
      }
    },
  }
}

export default plugin
