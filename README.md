# KiCad Schematic Style

面向 Codex 的个人 KiCad 原理图绘制、整理与审查 skill。将绘图偏好、可复用的原理图生成工具、电气身份验证和独立审查闭环放在同一个仓库中。

目标是让原理图从首次构图就具备清楚的功能关系和紧凑的局部，而不是连接网表后再把器件往中间挪。优先复用已有库符号，以真实引脚、字段和服务关系决定放置，完成全局组合后再选择图幅。

这是原理图工作流与工具集，当前通过文件生成和 KiCad CLI 工作；不需要安装 KiCad 插件，不提供 PCB 自动布局布线或编辑器自动刷新。

## 主要能力

| 部分 | 用途 |
| --- | --- |
| 绘图规范 | 功能分块、支持支路归属、端点直连、字段排布、电源符号及有方向依据的信号标签 |
| `sch_author.py` | 从 JSON 配方生成单页原生 `.kicad_sch`，按真实引脚定位，支持字段变体和完整功能组移动 |
| `schematic_check.py` | 采集原生网表、ERC、源文件与依赖快照，比较器件身份、属性、网络端点和作用域 |
| `style_probe.py` | 提供重叠导线、引脚覆盖、部分电源方向及页外锚点等几何候选，并列出覆盖缺口 |
| `review_gate.py` | 冻结当前证据、派发独立检查、校验报告，并追踪修复前后的问题 |
| 功能归属与校准材料 | 保存复杂任务的核心、支持关系及阅读链；区分已确认偏好和待认可的视觉候选 |

电源轨使用电源符号；信号标签依据真实供消关系设置角色。去耦、保护、偏置和反馈应能看出具体服务对象，同网不意味着可以任意归组。正常符号与字号参与布局，字段换位和支路改形先于无依据地扩大间距。

图幅不预设必须 A4 或 A5。先检查局部余量与全局组合，再试相邻较小图幅；候选存在未处理碰撞或纸外堆放，不能据此宣称较小图幅不可行。详细判据以 [功能布局规则](references/functional-layout.md) 为准。

## 安装与使用

可直接克隆公开仓库。以下是 Windows PowerShell 示例，将 skill 放入本项目当前使用的用户级技能目录：

```powershell
$styleRoot = Join-Path $env:USERPROFILE '.agents\skills\kicad-schematic-style'
git clone https://github.com/www666www6661/kicad-schematic-style.git $styleRoot
```

如果目标目录已有安装副本，先确认它是 Git checkout 还是复制安装，以及是否存在本地修改；不要直接覆盖。更新 Git 安装可在工作区干净时执行 `git -C $styleRoot pull --ff-only`。让 Codex 在后续任务中加载当前版本的 skill。

调用示例：

```text
$kicad-schematic-style 根据 requirements.md 绘制原理图，工程放到指定目录。

$kicad-schematic-style 整理现有 main.kicad_sch，保持电气连接和器件身份，
改善局部紧凑度、支路归属及全图阅读，并执行适用的独立检查。

$kicad-schematic-style 只读审查当前原理图，指出可读性和空间利用问题。
```

模型使用入口是 [SKILL.md](SKILL.md)。本 README 介绍安装与工具，不替代其中按操作加载的规则。选型、采购渠道或价格要求由具体任务提供，skill 不内置这些限制。

## 工具依赖

- 整体工具链使用 **Python 3.11+**；生成、探针及电气比较的核心逻辑使用标准库，审查入口使用 `tomllib`。
- 原生网表、ERC 和 PDF 导出需要目标版本的 **KiCad CLI**；绘图工具还需显式提供实际符号库路径。
- PNG 预览和审查证据包需要 **PyMuPDF**：在运行脚本的 Python 环境中执行 `python -m pip install PyMuPDF`。
- 默认独立审查需要已配置可运行的 **Codex CLI**。也可通过宿主提供的新上下文子 agent 完成检查，见后文。

本机验证环境为 Windows、Python 3.14 和 KiCad 10.0.5。其他环境需要替换路径并验证对应版本；源码中的能力边界和报告里的未验证状态仍然适用。

## 从示例开始

下例生成两个低侧 MOS 通道，用于检查工具链。示例是回归用例，不是完整产品设计，也不是经过用户认可的风格模板。

```powershell
$nativeCli = 'D:\app\KiCad\10.0\bin\kicad-cli.exe'
$symbolRoot = 'D:\app\KiCad\10.0\share\kicad\symbols'
$libraryArgs = @('--library', "Device=$symbolRoot\Device.kicad_sym",
                 '--library', "power=$symbolRoot\power.kicad_sym",
                 '--library', "Transistor_FET=$symbolRoot\Transistor_FET.kicad_sym")
$exampleOutput = Join-Path $env:TEMP ('kicad-style-example-' + [guid]::NewGuid().ToString('N'))

python -B -X utf8 "$styleRoot\scripts\sch_author.py" symbol 'Transistor_FET:2N7002' @libraryArgs
python -B -X utf8 "$styleRoot\scripts\sch_author.py" build "$styleRoot\references\authoring-examples\mos-driver.json" @libraryArgs --output $exampleOutput --kicad-cli $nativeCli --png
```

输出包含原理图、可再次生成的 `recipe.json`、实际端点与功能关系 `drawing.json`、`result.json`，以及原生 XML、ERC JSON、PDF 和 PNG。输出目录必须尚不存在。

构建成功不代表 ERC 清零或图面合格；查看 `result.json`、`erc.json` 和原生预览。生成器不自动决定拓扑、最佳朝向、碰撞避让、全局排列或纸张，也不导入重写任意已有工程。配方及字段接口见 [绘图工具说明](references/authoring-tools.md)。

## 独立检查与 battle

**battle 保留：主 agent 负责绘制和修复，独立检查 agent 负责提出问题及复查。** 它由主 agent 在规定节点调度，脚本本身不会自动修图或无限循环。

1. 局部绘制者完成原生自查和必要收紧，主 agent 完成全图组合自查。
2. 首次全图集成后执行 `--stage layout`；检查 agent 独立观察功能阅读、几何、紧凑度和适用的分页问题。
3. 主 agent 集中修复一批，回灌生成入口，并完成受影响的电气身份验证。
4. 用新输出目录和 `--previous-review` 提交新版本。检查 agent 先看新图，再逐项复查旧问题，同时检查回归。
5. 准备交付时执行 `--stage final`，附适用基线和图幅候选；首次检查已到最终版本时可直接从 final 开始。

默认每阶段最多三批修复与复查；连续两轮同一问题无实质进展或在排法间反复时也会停止自动尝试。达到上限不等于通过，仍需报告具体剩余项。用户要求继续时可以继续。

```powershell
$schematicPath = 'C:\task\project\main.kicad_sch'
$requirementsPath = 'C:\task\requirements.md'

python -B -X utf8 "$styleRoot\scripts\review_gate.py" run $schematicPath --stage layout --requirements $requirementsPath --kicad-cli $nativeCli --output 'C:\task\checks\layout-01'

# 主 agent 修复并验证后，提交当前版本；不要覆盖上一轮目录。
python -B -X utf8 "$styleRoot\scripts\review_gate.py" run $schematicPath --stage layout --requirements $requirementsPath --kicad-cli $nativeCli --previous-review 'C:\task\checks\layout-01' --output 'C:\task\checks\layout-02'
```

`requirements.md` 只提供原始需求和有效约束，不装入作者的“已经修好”等自评。默认每轮使用新的 `codex exec --ephemeral --sandbox read-only`，不 resume 或 fork 作者会话；模型沿用 CLI 配置，未固定型号。新进程和受控输入包减少历史影响，但共享文件系统不构成完全的信息权限隔离。

宿主子 agent 方式使用 `prepare` 生成 `dispatch.json`，以其中固定消息和 `fork_turns="none"` 派发，将原始报告交给 `finalize`。没有独立执行器时保留未验证状态。完整参数、隔离边界、图幅候选和停止条件见 [审查入口](references/review-gate.md)。

## 如何读结果

电气身份、图面质量、紧凑度以及图幅选择分别判断。网表等价不证明电路设计正确；无碰撞不证明紧凑；缩小一级纸张也不代表任务完成。

| 审查状态 | 含义 |
| --- | --- |
| `AWAITING_REVIEW` | 证据包已准备，尚无独立图面结论 |
| `REVIEW_ACCEPTED` | 当前阶段与范围的视觉复核接受，仍需读取电气结果与未验证项 |
| `CHANGES_REQUESTED` | 有具体问题需要修复后复查 |
| `UNVERIFIED` | 证据、覆盖或执行不完整；已确认的问题不会因此撤销 |
| `STALE` | 受审源、规则或依赖已变，旧结论不能用于当前版本 |

同时读取 `review.assessments`、`findings` 和 `rechecks`，不能只看顶层状态。探针零候选不等于通过；一般字形碰撞、功能归属、全图构图仍依赖实际查看原生图面，独立模型的判断也可能有遗漏。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [绘图约定](references/drawing-conventions.md) | 符号、字段、电源、标签与导线 |
| [功能布局](references/functional-layout.md) | 首轮紧凑构图、有限收紧、全局组合和选纸 |
| [绘图工具](references/authoring-tools.md) | 配方、真实引脚、字段变体及功能组操作 |
| [编辑与验证](references/editing-and-validation.md) | 基线、影响分析、网表身份比较及原生图面复核 |
| [独立审查](references/review-gate.md) | battle 调用、报告、复查和停止条件 |
| [归属记录](references/layout-contract.md) | 复杂任务中的核心、支持关系和端口交接 |
| [校准材料](references/calibration.md) | 已确认的组织偏好与尚待认可的候选 |

## 开发与验证

在仓库目录运行测试。设置 `KICAD_CLI` 和符号库目录可启用需要本机 KiCad 的集成检查；缺少依赖时部分原生测试会跳过，不能将跳过算作通过。

```powershell
$env:KICAD_CLI = 'D:\app\KiCad\10.0\bin\kicad-cli.exe'
$env:KICAD_SYMBOL_DIR = 'D:\app\KiCad\10.0\share\kicad\symbols'
python -B -X utf8 -m unittest discover -s tests -v
```

当前版本在上述本机环境完成 238 项测试，包含原生库、端点、身份和审查入口回归。这些测试验证工具行为，不给出自动绘图的审美成功率，也不能替代具体工程的图面验收。
