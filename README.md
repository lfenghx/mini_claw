## Mini Claw

**Author:** lfenghx  
**Version:** 1.1.0
**Type:** Tool (Tool Plugin)

### Overview

Mini Claw is a lightweight “little lobster” on the Dify platform—an AI assistant with a “soul”. It supports short-term and long-term memory, plus identity/personality/soul settings. It aims to help you feel the warmth of AI—come and get a dedicated AI assistant for you and your company.

Mini Claw follows the “Skill Progressive Disclosure” execution model: it treats the plugin’s `skills/` directory as a toolbox, so the Agent can read the skill manual step-by-step and then read files / run scripts as needed, finally delivering text or files.

### Use Cases

- You want to quickly experience the popular “little lobster”
- You want to build your own AI assistant with a soul that gets better the more you use it
- You want to extend capabilities through skill packages

### Key Features

- Soulful AI: includes identity, soul, and user profiles; adapts behavior based on user input to provide personalized service
- Skill invocation: checks a skill index first, then reads `SKILL.md`, then reads files / executes commands as needed
- Free execution: the Agent can execute commands, including reading/writing files and running scripts
- Skill management: supports skill CRUD (view/add/delete), plus dependency detection and dependency installation

### Tools

This plugin provides two tools:

- “Mini_Claw”: a soulful AI assistant for conversation and task execution. It has short-term and long-term memory, identity/personality/soul, and adapts to user input for personalized service.
- “Skill Management”: manages the skills directory. You can view/add/delete skills, and run dependency detection/installation.

  ![alt text](_assets/image_0.png)

### How to Use (in Dify)

1. Install this plugin from the marketplace.
2. For self-hosted users: set `Files_url` in Dify’s `.env` to your Dify address (restart Dify afterward), otherwise Dify may not be able to fetch uploaded files.
3. Build a workflow like the example below:

   ![alt text](_assets/image_1.png)
   ![alt text](_assets/image_1-1.png)
   ![alt text](_assets/image_1-2.png)

4. Chat with Mini_Claw and set up a persona:

   ![alt text](_assets/image_2.png)

Tips:
- Tip 1: use the `update_persona` tool to adjust identity and soul (`SOUL.md`) to make your Mini_Claw feel more “alive”.
- Tip 2: send the command “重置角色” (reset role) to reset Mini_Claw’s identity, clear memory, and start over.

5. Use Skill Management to extend Mini_Claw with custom tools (add/delete/list skills, availability checks, dependency checks/installation):

   ![alt text](_assets/image_3.png)

Feature 1: Built-in dependency detection and installation. The Agent is no longer allowed to install dependencies by itself. Please make sure dependencies are installed before use.

- 🔴 Not available: dependencies must be preinstalled. Use “Dependency Install” to install what can be installed automatically; for anything that cannot be installed automatically, install it manually in the `plugin_daemon` container.
- 🟡 Uncertain: the skill’s YAML Front Matter is not standardized, so availability cannot be determined.
- 🟢 Available: dependency checks pass. If the skill body still contains extra installation instructions, the skill may appear “available” but still fail at runtime. Do not put dependency requirements in the body—declare them in metadata instead.

Feature 2: This project is compatible with OpenClaw’s skill directory structure. We strongly encourage everyone to standardize skills and define skill metadata (name, description, triggers, required environment, etc.) via YAML Front Matter.

Example of a standard `SKILL.md` YAML Front Matter:

```yaml
---
name: agent-browser
description: Use the agent-browser CLI for headless browser automation: open pages, interact, export screenshots/PDF.
read-when: When the user mentions “webpage screenshot / open page / headless browser / automation click & type”
metadata: {"openclaw":{"os":["linux","darwin","win32"],"requires":{"bins":["agent-browser","node","npm"],"env":[]}}}
---
```

- `name`: skill name
- `description`: detailed description of the skill
- `read-when`: trigger conditions for the skill
- `metadata`: skill metadata, including OS constraints and dependencies

This version no longer forces `name` to be the same as the skill folder name, but it is still recommended to keep them consistent.

Feature 3: For Python skills, include `requirements.txt`. For Node.js skills, include `package.json` (or provide `node_modules`).

Feature 4: The Dify `plugin_daemon` container does not include Node.js by default. Install it yourself. Some CLI skills require you to install required tools in `plugin_daemon`.

### Core Usage

1. With persistent conversations, Mini_Claw learns more about you over time and builds long-term collaboration.
2. Use Skill Management to add custom tools. Install dependencies properly so Mini_Claw can execute smoothly.
3. Mini_Claw is isolated at the app level: contexts are shared within the same app.

### Release Notes

- 1.0.0: Mini Claw official release

### FAQ

1. Installation fails
   - If you have network access but installation still fails, try switching Dify’s pip index/mirror for better dependency downloads.
   - In an intranet/offline environment, you may need offline packages; contact the author.

2. File transfer issues
   - If upload/download fails (wrong URL, timeout, etc.), check whether `Files_url` in Dify’s `.env` is set correctly and matches your Dify address.

3. Mini_Claw produces no output
   - This is usually a model capability issue. Make sure your model and provider plugin support function calling. Switching models or upgrading the provider plugin often fixes this.

4. Skill invocation
   - The more complete the skill package, the smoother the Agent invocation. Ensure skill files and scripts are present and follow the standard format.

### Author & Contact

- Bilibili tutorial: https://www.bilibili.com/video/BV1sjwFzbENN
- GitHub: lfenghx (repo: <https://github.com/lfenghx/mini_claw>)
- Bilibili: 元视界_O凌枫o
- Email: 550916599@qq.com
- WeChat: lfeng2529230
