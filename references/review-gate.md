# 预配置独立检查入口

主 agent 使用 `scripts/review_gate.py`，不临时重写审查提示词。固定职责在 `reviewer-prompt.md`，输出结构在 `review-report.schema.json`；它们由脚本装入当前版本的审查任务。

## 何时触发

| 节点 | 操作 |
|---|---|
| 新建/整体整理的全图首次集成，主链和支持组已经形成 | `run --stage layout`，先发现构图方向和组内松散问题，再做后续精修 |
| 有限收紧、图幅选择及修复完成，准备交付 | `run --stage final`；若首次检查时已到最终版本，只跑 final 一次 |
| 同一受审版本检查结束后 | 读取 `gate-result.json` 的问题与未验证项，主 agent 修复；版本变化后重新采集，旧报告不替新版本背书 |
| 既有图局部修改 | 小字段微调按原流程自查；需要独立复核时用 `--scope local --affected-ref R12`，可重复指定受影响位号 |

这里是明确节点上的调用入口，不是后台监听器，也不触发编辑器刷新。检查脚本只读工程，修图仍由主 agent 执行。不要自行加入“循环调用直到通过”；停止条件仍按布局细则，未完成项如实报告。

## 默认的一次调用

需要 Python、PyMuPDF、目标版本 `kicad-cli` 和已配置可运行的 Codex CLI。未指定 `--model` 时沿用用户的 CLI 模型配置，不固定模型、价格或供应商。`--requirements` 给原始功能需求和用户有效约束，不放主 agent 的自评、历史缺陷清单或聊天总结。

```powershell
$styleSkillRoot = 'C:\Users\wuji\.agents\skills\kicad-schematic-style'
python -B -X utf8 "$styleSkillRoot\scripts\review_gate.py" run 'C:\task\main.kicad_sch' --stage layout --requirements 'C:\task\requirements.md' --kicad-cli 'D:\app\KiCad\10.0\bin\kicad-cli.exe'
```

默认新建临时输出目录并立即打印其路径。也可用 `--output` 指定尚不存在的目录。调用会：

1. 复用 `schematic_check` 采集当前源、依赖、原生 XML/ERC/PDF；用冻结源逐页跑几何探针。
2. 只装入当前需求、绘图规则、冻结源、PDF 和整页/细节图片；不装入校准案例、历史报告或作者结论。
3. 用固定提示词启动新的 `codex exec --ephemeral --sandbox read-only`，不 resume/fork 作者会话；本进程关闭已配置 MCP、插件和自动 skill，并清除父会话 ID，不修改用户配置文件或认证。首轮看图后再看探针候选。
4. 收取结构化报告，核对受审版本、输入哈希、图面覆盖声明和现场是否变化，生成 `gate-result.json`。日志和原始响应留在本次输出目录。

默认单次审查超时 900 秒，可用 `--timeout` 调整；超时会结束本次启动的进程树并保留未验证状态。宿主调度此命令时使用可轮询的后台会话，不阻塞用户沟通。失败不自动重试、不切模型；排除运行问题后可另开一次审查。

## 电气基线和图幅证据

- 既有图整理用 `--before <修改前的快照目录>` 自动附带全图身份/网表/ERC 比较。无基线时明确未验证，不能凭视觉结论声称电气等价。
- 最终图幅需要较小候选证据时，先按布局细则做一次候选并用 `schematic_check.py snapshot ... --pdf` 留下快照，再通过 `--paper-candidate <候选快照目录>` 交入。脚本只装入候选源、原生图面及比较事实，复核者在形成当前图独立意见后再查看；不接受“缩小失败”一句话代替试排。
- 用户明确指定纸张才用 `--fixed-paper`。局部修改和已到 A5 的常规任务可有图幅例外，仍要看实际边界。没有候选证据且不属于例外时，final 的图幅结论保持未验证。
- 编号冻结源用于读证据，不是可重新导出的项目副本；不要对它运行生成器或拿它冒充完整层级工程。

## 无外部 CLI 时的固定交接

不需要主 agent 自己写提示词。把上例 `run` 换成 `prepare`，读取输出目录中的 `dispatch.json`，将其 `task_name`、`fork_turns="none"`、`message` 原样交给宿主的 `spawn_agent`。不补充作者解释或预期答案。子 agent 只返回符合 schema 的 JSON，主 agent 原样保存到临时响应文件，再调用：

```powershell
python -B -X utf8 "$styleSkillRoot\scripts\review_gate.py" finalize '<本次输出目录>' --report '<子agent原始JSON响应文件>' --reviewer-id '<实际子agent ID>'
```

已准备的包也可以用 `review_gate.py review <输出目录>` 启动外部 CLI。没有可用的独立执行器时保留未验证，不让主 agent 代填审查者的通过结论。原生子 agent 的新上下文由宿主调用保证，脚本不能替宿主证明它没有继承历史。

## 结果怎么用

| 状态 / 退出码 | 含义与后续 |
|---|---|
| `AWAITING_REVIEW` / 0 | prepare 成功，尚无人工图面结论 |
| `REVIEW_ACCEPTED` / 0 | 本阶段、本范围的视觉复核接受；继续读取电气比较和未验证项，不是整项工程认证 |
| `CHANGES_REQUESTED` / 1 | 复核指出需处理的问题；主 agent 按具体证据修复 |
| `UNVERIFIED` / 2 | 响应缺失/运行失败、覆盖不足或必要证据不足；已有问题仍保留在报告内 |
| `STALE` / 3 | 当前源、需求、规范、依赖或工具已变化；重采当前版本，不复用旧通过结论 |

每次读取结果中的 `stage`、`scope` 和 `affected_refs`。整页 PNG 与四个细节区域的查看记录是审查者声明，脚本能查缺项，不能证明模型真看准了。报告声明读到包外任务材料时按未验证处理。共享文件系统及 CLI 全局指令仍可能提供其他信息，输入包和新进程都不等于完全的读取权限隔离。规则文件更新也会使进行中的审查过期。
