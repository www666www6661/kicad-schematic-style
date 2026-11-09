# KiCad Schematic Style

一套面向 Codex 的 KiCad 原理图绘制规范和辅助工具，用于新建原理图、整理已有工程，以及检查连接关系和图面可读性。

重点是功能清楚、布局紧凑：保护器件靠近被保护器件，去耦和反馈支路能看出连接对象，位号与数值摆放清晰，完成布局后再选择合适的图幅。

## 功能

- **绘制原理图**：优先使用已有库符号，生成可在 KiCad 中继续编辑的 `.kicad_sch` 文件。
- **整理布局**：按功能组织电路，调整器件、文字和连线，减少不必要的空白与绕线。
- **统一表达**：规范电源符号、信号标签、位号、数值及连接点的使用。
- **检查修改**：比较整理前后的器件、属性和网络连接，并检查 ERC 差异。
- **独立复核**：由未参与绘制的 agent 检查当前图面，修正问题后再次复核。

## 安装

以 Windows PowerShell 为例：

```powershell
$styleRoot = Join-Path $env:USERPROFILE '.agents\skills\kicad-schematic-style'
git clone https://github.com/www666www6661/kicad-schematic-style.git $styleRoot
```

已有安装时保留本地修改；通过 Git 安装的副本可在工作区干净时使用 `git -C $styleRoot pull --ff-only` 更新。

运行辅助工具需要：

- Python 3.11 或更高版本。
- KiCad CLI，用于导出网表、ERC 报告和 PDF；绘图工具还需访问 KiCad 符号库。
- PyMuPDF，用于生成图片预览：`python -m pip install PyMuPDF`。
- 独立复核默认通过已配置的 Codex CLI 运行，也支持宿主提供的独立子 agent。

无需安装 KiCad 插件。当前验证环境为 Windows、Python 3.14、KiCad 10.0.5。

## 使用

在 Codex 中指定 skill、需求文件和工程位置即可：

```text
$kicad-schematic-style 根据 requirements.md 绘制原理图，保存到 D:\Projects\Example。

$kicad-schematic-style 整理 D:\Projects\Example\main.kicad_sch，
保持器件和电气连接不变，让布局更紧凑、文字和连线更清晰。

$kicad-schematic-style 只读审查当前原理图，指出可读性和空间利用问题。
```

如有指定器件、纸张或选型要求，在任务中一并说明。默认先整理电路布局，再选择图幅。

绘制完成后会结合 KiCad 原生导出检查图面，并根据修改范围核对电气连接。新建和整体整理任务包含独立复核：检查者查看当前图纸，绘制者修正问题，再提交复查。具体调用方式见 [检查流程](references/review-gate.md)。

## 工具与文档

通常由 Codex 按 [SKILL.md](SKILL.md) 调用所需工具。需要单独运行脚本或调整规则时，可查阅以下文档：

| 文档 | 内容 |
| --- | --- |
| [绘图约定](references/drawing-conventions.md) | 符号、字段、电源、标签与导线 |
| [功能布局](references/functional-layout.md) | 功能分块、器件间距、整页布局与图幅选择 |
| [绘图工具](references/authoring-tools.md) | `sch_author.py` 用法与双通道 MOS 示例 |
| [编辑与验证](references/editing-and-validation.md) | `schematic_check.py`、`style_probe.py` 与修改前后检查 |
| [检查流程](references/review-gate.md) | `review_gate.py` 的独立检查与复查 |

当前绘图脚本支持按 JSON 描述生成单页原理图，布局仍由 agent 决定。项目专注原理图，不包含 PCB 布局布线；检查结果也不能替代电路设计验证和实物测试。

## 测试

在仓库目录运行，KiCad 路径按实际安装位置修改：

```powershell
$env:KICAD_CLI = 'D:\app\KiCad\10.0\bin\kicad-cli.exe'
$env:KICAD_SYMBOL_DIR = 'D:\app\KiCad\10.0\share\kicad\symbols'
python -B -X utf8 -m unittest discover -s tests -v
```

测试覆盖符号处理、原生网表、器件身份比较和检查流程。未配置 KiCad 或符号库时，部分集成测试会跳过。
