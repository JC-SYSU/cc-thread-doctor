# Claude Code Thread Doctor

[English](README.md)

诊断并修复无法 resume、每次请求都被供应商 schema 校验拒绝、或者大到 resume 很慢的 Claude Code 会话文件。

所有修复都先备份、原子写入、再从磁盘读回验证；需要时可以把会话复制成新 id，用真实的 `claude` CLI 做一次影子 resume 来证明修好了。

状态：v0.1.0，早期版本。audit 和当前两个可用治疗都有测试覆盖；其余失败模式记录了证据和计划中的命令，并明确标注了状态。

## 起因

把一个几个月的旧会话 `--resume` 到 DeepSeek / 智谱端点后，每条消息都返回：

```text
API Error: 400 Invalid schema for function 'Artifact':
"^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}\"\\\\./[\]]{1,200}$" is not a "regex"
```

该会话录制于 Artifact 工具还启用的时候。`settings.json` 里已经设了 `CLAUDE_CODE_DISABLE_ARTIFACT=1`，新会话也确实干净，但 `--resume` 会重放会话 `prompt_snapshot` 里记录的工具定义。Artifact 的 schema 带负向前瞻，供应商用 Go/RE2 校验，编译不了，整个请求被拒。

修复办法是从快照里删掉 Artifact。对 229 个会话做全库扫描时，又找出 6 个带着同一份工具定义的会话——它们下次 resume 都会撞上同一个 400。

## 工作方式

```text
audit（只读） ──► 备份 + SHA-256 ──► fix（原子、未动行保持原字节） ──► verify（静态 + 影子 resume）
```

- **audit** 不写文件。可扫单个会话或全库，只报机器状态层面的问题。
- **preserve** 在任何改动前复制文件、校验副本哈希、写 manifest。
- **treat** 只改命令点名的记录，其余行按原始字节写回，未知和未来新增的记录类型也不会被洗掉。
- **verify** 重新读取文件；加 `--shadow-resume` 时会把会话复制成新 id，用真实 CLI resume 一次，然后删除副本。

## 能修什么

已有测试和真实案例验证：

| 故障 | 命令 |
| --- | --- |
| 快照里记录了不该再发送的工具（如关闭 Artifact 后仍带 Artifact） | `doctor_fix.py tools --drop Artifact --apply` |
| 任意工具 schema 的 `pattern` 含 RE2 不支持的结构 | `doctor_fix.py tools --drop <tool> --apply` |
| 崩溃导致的尾部截断行（末尾不是合法 JSON） | `doctor_fix.py repair-tail --apply` |

v0.1 只检测、尚未自动治疗：

| 故障 | 状态 |
| --- | --- |
| `tool_use` 没有配对的 `tool_result`（回合被中断） | audit 报 `dangling-tool-use` / `unanswered-final-turn` |
| 会话过大（内联图片、巨型 tool result、请求 blob） | audit 报 `oversized-session` 并给出体积归因 |
| 孤儿 `tool_result`、未知记录类型 | audit 报出 |

## 快速开始

```bash
# 全库扫描，只读
python3 scripts/doctor_audit.py --all

# 单个会话，机器可读
python3 scripts/doctor_audit.py --session abc12345 --json

# 治疗：先 dry run，再 --apply
python3 scripts/doctor_fix.py tools --session abc12345 --drop Artifact
python3 scripts/doctor_fix.py tools --session abc12345 --drop Artifact --apply

# 验证：静态，或花 token 做影子 resume
python3 scripts/doctor_verify.py --session abc12345
python3 scripts/doctor_verify.py --session abc12345 --shadow-resume --allow-cost
```

退出码：`0` 干净或 dry run，`1` 有问题需要处理，`2` 工具自身失败。`doctor_audit.py --strict` 会把 warning 也算失败。

## 数据与安全

- 备份在 `~/.claude/.cc-thread-doctor-backups/<session>-<时间戳>/`，带 SHA-256 manifest；`doctor_fix.py restore` 可以放回去。
- 每次写入都是同目录临时文件加 `os.replace`；断言失败会自动还原备份并以 2 退出。
- 不加 `--apply` 不写任何东西；会话 id 出现在运行中的 `claude --resume` 进程里时，除非 `--force`，否则拒绝写入。
- 报告只留在本机，不联网。影子 resume 是唯一会联系供应商的操作，走你现有的 CLI 配置。

## 限制

- 影子 resume 只能证明本机当前供应商接受这个会话，不能保证远端永远接受。
- JSONL 是内部格式，版本之间会变。v0.1 基于 Claude Code 2.1.304 开发；未知记录类型会原样保留并报告，但将来格式变化可能需要更新读取器。
- 最后一条 assistant 消息里未配对的工具调用按 warning 报：回合运行中属于正常现象，只有会话被中断时才会破坏请求。
- 不修改 Claude Code 本体、hooks 或认证。

## 测试

```bash
python3 -m pytest -q
```

测试全部使用合成会话，不包含任何真实 transcript。

## 目录

```text
scripts/doctor_lib.py       会话读取、检查、备份与原子写入
scripts/doctor_audit.py     只读扫描
scripts/doctor_fix.py       治疗与还原
scripts/doctor_verify.py    静态与影子验证
references/session-jsonl.md 文件里有什么、resume 会重放什么
references/failure-modes.md 症状、证据、治疗、状态
```

## License

MIT。
