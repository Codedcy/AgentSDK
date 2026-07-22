# Bilingual README Design

## Goal

Add a complete Simplified Chinese README while keeping the existing English
README as the repository's default GitHub entry point. A reader must be able to
switch languages from the top of either file without losing installation,
capability, safety, recovery, or maintenance information.

## Reference Pattern

The design follows the established repository-local language-file pattern used
by Chinese open-source projects such as Ant Design and Dify:

- keep one default `README.md`;
- store each translation as a separate Markdown file;
- put explicit language links near the top of every version;
- keep examples and factual contracts aligned across languages.

References:

- <https://github.com/ant-design/ant-design>
- <https://github.com/langgenius/dify>

The project will use plain text language links rather than badges because the
current README deliberately avoids unsupported external badge contracts.

## File Structure

- `README.md` remains the canonical English landing page.
- `README.zh-CN.md` is the complete Simplified Chinese version.

Immediately below the top-level project title, each file contains the same
two-language selector:

```markdown
English | [简体中文](README.zh-CN.md)
```

The Chinese file reverses the active and linked states:

```markdown
[English](README.md) | 简体中文
```

Both links are repository-relative so they work on GitHub, in local Markdown
viewers, and in source distributions.

## Translation Scope

`README.zh-CN.md` is a full translation, not a shortened regional landing page.
It mirrors the English section order:

1. project positioning;
2. reasons to use Agent SDK;
3. source installation;
4. deterministic smoke run;
5. real LiteLLM-backed Agent example;
6. v0.1 capability matrix;
7. Tool and permission examples;
8. MCP connection;
9. generated Workflow admission;
10. observation and recovery;
11. v0.1 boundaries;
12. documentation navigation;
13. development and verification.

Commands, Python examples, file paths, model identifiers, API symbols, version
numbers, test evidence, and relative documentation links remain byte-for-byte
equivalent wherever translation is not required. Explanatory prose, headings,
table labels, and list text are translated.

## Terminology

Public API names are never translated. The Chinese prose uses these stable
terms:

| Source term | Chinese prose |
| --- | --- |
| Agent Loop | Agent 循环 |
| Tool | 工具（Tool） on first use, then Tool |
| Workflow | 工作流（Workflow） on first use, then Workflow |
| Child agent | 子 Agent（Child Agent） on first use, then Child Agent |
| Trace | 追踪（Trace） on first use, then Trace |
| Context | 上下文（Context） on first use, then Context |
| Run | Agent Run or Run, matching the public API concept |
| Session | Session, matching the public API concept |

LiteLLM, MCP, SQLite, Skill, Prompt Manifest, API class names, error codes, and
compaction levels L0-L4 remain unchanged. This preserves searchability and avoids
inventing a second vocabulary for SDK concepts.

## Source of Truth and Synchronization

`README.md` remains the canonical content source. The Chinese README is manually
maintained in the same change whenever English public behavior changes. v0.1 does
not introduce a translation generator, CI translation service, or duplicated
structured source format.

Tests protect facts that can be compared mechanically, while review remains
responsible for translation quality. This deliberately keeps the implementation
small and does not turn README maintenance into a documentation build system.

## Validation Contract

Extend `tests/docs/test_public_readme.py` so it validates both files:

- both README files exist and link to each other at the top;
- the English file marks English active and links Simplified Chinese;
- the Chinese file links English and marks Simplified Chinese active;
- the Chinese version contains the source-install command, deterministic smoke
  command, version/Python support, capability matrix, recovery boundary,
  generated Workflow admission boundary, and all five documentation links;
- commands and relevant code blocks are unchanged between languages;
- every Python block in both files compiles as a normal Python module;
- neither language claims a package-index release or unsupported badges;
- the historical full-suite count remains labelled as a release checkpoint.

The focused documentation suite, Ruff, strict mypy, smoke reference, and
`git diff --check` remain the completion gate. The runtime implementation is not
changed by this feature.

## Error Prevention

The language selector uses direct file links, so it has no runtime error path.
The primary maintenance risks are missing reciprocal links and translation
drift. Contract tests fail on missing key sections, commands, API examples, or
links. Review must still catch semantic mistranslation that cannot be established
by string comparison.

## Non-goals

- making Chinese the default GitHub README;
- translating the detailed documents under `docs/`;
- adding Traditional Chinese or other languages;
- adding language badges or an external translation service;
- generating one README from the other;
- changing runtime APIs or v0.1 capability claims.
