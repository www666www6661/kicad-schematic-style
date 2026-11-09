# 原理图绘图工具

`scripts/sch_author.py` 是首轮绘制的可复用工具，用 JSON 配方描述实际符号、引脚依附、字段、功能组和连线。它写出可编辑的原生 `.kicad_sch`，不需要给 KiCad 安装插件。新建图或新建局部优先使用已覆盖的操作，避免每个工程重新实现符号变换与文件序列化。

**适用边界：** 当前支持单页、正交旋转与镜像，库路径显式提供。它不导入或重写任意已有工程，不自动决定电路拓扑、符号朝向、字段是否放得下、最优间距、组间排列或纸张。已有工程继续用验证细则的保留身份编辑流程；不能为使用新工具重建原图的 UUID。电气模型与绘图规则仍由作者核实，工具输出不等于审美认可。

## 调用入口

示例使用已明确的两个低侧 MOS 通道，文件位于 [authoring-examples/mos-driver.json](authoring-examples/mos-driver.json)。这是工具回归题，不是经过用户认可的风格模板。Windows 示例路径按本机安装替换；生成器本身只依赖 Python 标准库，PNG 预览另需 PyMuPDF。

```powershell
$styleRoot = 'C:\Users\wuji\.agents\skills\kicad-schematic-style'
$nativeCli = 'D:\app\KiCad\10.0\bin\kicad-cli.exe'
$symbolRoot = 'D:\app\KiCad\10.0\share\kicad\symbols'
$libraryArgs = @('--library', "Device=$symbolRoot\Device.kicad_sym",
                 '--library', "power=$symbolRoot\power.kicad_sym",
                 '--library', "Transistor_FET=$symbolRoot\Transistor_FET.kicad_sym")

# 先查实际编号和库引脚偏移，不凭型号猜。
python -B -X utf8 "$styleRoot\scripts\sch_author.py" symbol 'Transistor_FET:2N7002' @libraryArgs

# 输出目录必须尚不存在；不覆盖之前的工程或检查证据。
python -B -X utf8 "$styleRoot\scripts\sch_author.py" build "$styleRoot\references\authoring-examples\mos-driver.json" @libraryArgs --output 'C:\task\draw-01' --kicad-cli $nativeCli --png
```

`build` 产物包括原理图、保存了实际库路径的 `recipe.json`、带实际引脚坐标和功能关系的 `drawing.json`、`result.json`。给出 CLI 时另导出原生 XML、ERC JSON、PDF；`--png` 逐页生成 PNG。实际打开预览做首轮自查；`visual` 保持 `UNVERIFIED`，程序不代填看图结论。

退出 `0` 表示构建完成且未发现已声明的端点差异；`1` 表示原生网表与 `expected_nets` 不符；`2` 表示输入/执行失败。创建输出目录后若原生导出失败，保留源图、配方与 `status: ERROR` 的 `result.json`，不能把其中的部分产物作为完成证据。没有预期端点表时电气意图为 `UNVERIFIED`；ERC 是否存在错误需另读 `erc.json`，不把构建退出零当作 ERC 通过。

## 配方与坐标

根字段：`schema_version: 1`、`name`（文件名）、`project_id`（工程 UUID）、`libraries`（库名到文件路径）、`paper`，以及 `groups/symbols/anchors/wires/labels/junctions/no_connects/supports/read_chains/expected_nets`。未知字段报错。库路径相对配方目录，命令行 `--library 名称=路径` 可覆盖；不搜索或改动共享库。

`project_id` 为新工程一次生成并保持的 UUID。同一对象的 `id`、器件 `ref/unit` 和工程 UUID 在整理时保持，坐标不参与符号或引脚 UUID。不要复制示例的工程 UUID 用于不同实际工程。保持文件名时通常也保持 `name`；配方是本工具后续再生成的依据，直接手改导出图后须回灌或停用旧配方。

```json
{
  "schema_version": 1,
  "name": "example",
  "project_id": "eb38f020-e656-4768-aac4-5675378d8efe",
  "libraries": {},
  "groups": [{"id": "filter", "origin": [50.8, 50.8]}],
  "symbols": [
    {"id": "R1", "ref": "R1", "lib_id": "Device:R", "value": "1k",
     "group": "filter", "at": [0, 0], "rotation": 90,
     "fields": {"Reference": {"visible": false}, "Value": {"layout": "inside"}}},
    {"id": "C1", "ref": "C1", "lib_id": "Device:C", "value": "100n",
     "group": "filter", "attach": {"pin": "1", "to": {"pin": ["R1", "2"]}},
     "fields": {"Reference": {"layout": "right"}, "Value": {"layout": "right"}}}
  ]
}
```

该片段只演示端点定位，非完整电路。R1 横向放置；C1 的引脚 1 直接贴 R1 引脚 2，程序按实际库与变换反算 C1 原点，不生成零长度 wire。`attach.offset` 是纸面方向上有用途的偏移，默认 `[0,0]`；非零偏移不自动产生导线，连接仍须用 `wires` 表达。

坐标均为 mm，内部用 Decimal。符号 `at` 相对于其 `group` 原点，无 group 时为纸面坐标。组可通过 `parent` 嵌套，原点相对父组。`rotation` 支持 0/90/180/270，`mirror` 为 `x` 或 `y`；`unit/body_style` 指定实际库单元。库继承先展开，未知或有歧义的编号、循环依赖报错，不猜位置。

符号 `in_bom/on_board/dnp/exclude_from_sim` 仅接受布尔值；普通器件默认依次为 `true/true/false/false`，`#` 虚拟器件的前两项默认 `false`。同位号多单元必须使用相同库符号、器件属性和标志；不一致时报错。生成器显式写出仿真排除标志，避免原生保存时补默认值形成属性差异。

用于导线、标签和依附的端点可以写为：

| 表达 | 含义 |
|---|---|
| `[x,y]` | 普通连线中为绝对坐标；在符号 attach 中为所属组的局部坐标 |
| `{"at":[x,y],"group":"filter"}` | 明确的组内坐标 |
| `{"pin":["C1","1"]}` | 当前变换后的真实引脚 |
| `{"anchor":"sense"}` | 引用根 `anchors` 中命名的端点表达 |
| `{"x":端点A,"y":端点B}` | 取 A 的 X、B 的 Y，形成随器件更新的正交拐点 |
| 上述对象加 `"offset":[dx,dy]` | 在纸面方向偏移 |

符号依附的所有引用须属于同一组，跨组通过连线或标签连接；这样移动任意一组不会留下仍被另一组位置锁住的符号。`attach` 只定义几何，不能自行代替网表核对。功能需要一起移动的支路从一开始归到完整组中，命名端点尽量使用 pin 或显式 group，避免悬空的绝对拐点。

## 字段

字段配置在符号 `fields` 中，按字段名逐项覆盖默认，不必再遍历导出文件改节点。`properties` 设置实际封装、数据手册及其他属性值；`ref` 独占位号定义。仅复用符号时，器件型号、说明和数据手册须对应真实选型。

- `layout: left/right/above/below`：按当前朝向的图形边界放置正常横读的位号和值。它提供初始候选，不做字形碰撞保证。
- `layout: inside`：仅支持标准 `Device:R` 的 Value，居中沿长轴；原生字形容纳情况仍需查看，放不下就外置。
- `offset: [dx,dy]`：相对符号原点的纸面偏移，与 `layout` 二选一。
- `align: left/right/center`、`orientation: horizontal/vertical`、`size`、`visible`、`value`：控制对应字段。垂直字段当前只支持居中；未指定字号为 1.27 mm。

工具转换 KiCad 的字段原始角度与左右对齐，使用者按纸面意图填写。字段位移不改变电气端点。虚拟 `#` 位号默认隐藏，PWR_FLAG 值默认隐藏。电源符号仍是 `symbols` 中的真实库实例；不要用 global label 冒充电源。方向与名称位置按绘图规范选择。

## 母线、支路与端口

`wires` 每项有稳定 `id` 和至少两个 `points`。`route: straight` 要求各段正交；指定 `hv/vh` 时为每对端点添加一个随端点更新的折角。程序不做障碍避让，不能把 hv/vh 当成自动布线。

并联支路先用 `attach` 将各自实际端点排在同一母线上，再以这些 pin 作为 wire 的 points。不强制同本体中心或统一引脚长度。工具清理零长度/重复/共线重叠线，在导线端点、真实引脚及标签接入处拆分母线，给明确 T 接添加接点。纯内部十字交叉不自动连接；需要四路连接时在 `junctions` 明确列出端点。原生网表仍是连接验证依据。

`labels` 包含 `id/name/at/kind/side`，全局标签 `kind: global` 还必须显式给 `role: input/output/bidirectional/tri_state/passive`；`side` 是文字向左/右伸展，独立于电气角色。局部标签无 role。`no_connects` 只接受实际 pin，拒绝在已接导线、标签或多引脚重合处添加 NC。

## 功能组与移动

`supports` 记录 `id/members/serves/role`，`serves` 为实际 pin 端点列表。`read_chains` 是按实际 symbol id 表达的阅读顺序。它们随配方保存并进入 `drawing.json`，用于主 agent 的改形与归属复核；工具检查对象存在，不据此声称摆放正确或自动推导 PCB 距离。

```powershell
python -B -X utf8 "$styleRoot\scripts\sch_author.py" move-group 'C:\task\draw-01\recipe.json' --group 'channel1' --delta 12.7 0 --output 'C:\task\moved.json'
python -B -X utf8 "$styleRoot\scripts\sch_author.py" build 'C:\task\moved.json' --output 'C:\task\draw-02' --kicad-cli $nativeCli --png
```

该命令仅修改新配方中的组原点。重建时器件、字段、依附端点、标签和引用它们的导线一起重算；没有旧坐标缓存。跨组导线会重新求端点，若固定折角导致斜线则报错，显式 hv/vh 可重新成折线；移动后的碰撞、交叉和全图阅读仍需核对。组内压缩应调整 attach/字段/支路形状，不能用整组平移冒充内部收紧。

图幅候选使用同一内容版本的独立配方，在其中明确每组最终原点与必要改形。`paper` 只影响图框，不驱动放置；工具默认值或早期预览的纸张都不代表已选定交付图幅。`move-group` 成功只证明坐标操作完成，候选仍按[布局细则](functional-layout.md#内容稳定后选图幅)判断试排是否充分。

## 电气意图与验证

编写配方前先完成[实际器件、符号与封装的端子对应](drawing-conventions.md#实际器件符号与封装的端子对应)。`symbol` 命令只列出所选库符号的引脚，当前工具不读取实物资料或封装焊盘作完整性校验；缺少的安装、屏蔽等端子不会由生成器自动补出。预期连接依据原始器件资料和电路要求建立，明确不连接的端子按其语义保留在符号及适用的 `no_connects` 中，不靠删脚使网表一致。

`expected_nets` 各项包含 `pins: [[symbol_id,pin],...]` 和可选 `name`。它是依据原始电路需求独立给出的端点表，覆盖所有原生导出的非虚拟电源器件网络，不能从生成后的网表倒填。无 name 时比较端点集合；有 name 时还比较 KiCad 实际网名（局部标签可能带 `/` 前缀）。集合须非空且互不重叠，缺网、合网、拆网及错误名称返回 DIFFERENT。电路正确性、BOM 参数与 ERC 仍分别核对。

整理同一配方时还用既有 `schematic_check.py` 做身份与网表差异检查。首次工具或 KiCad 版本变化按验证细则做原生编辑器副本往返；仅 CLI 导出成功不证明编辑器打开/保存稳定。该工具尚未实现实时编辑器读写或自动刷新。

本版已在本机 KiCad 10.0.5 验证示例的打开、保存、重开；往返后的身份、层级、属性、网络端点、NC、标签作用域及 ERC 差异均通过。这个结果覆盖该示例和版本，不替代其他符号、版本或实际工程的验证。

上游 KiCad 10 的 IPC 插件仅覆盖 PCB；本机华秋版另带原理图 MCP，协议及能力不同。本版工具不配置它。未来编辑器适配必须保留真实库 ID、完整网表与身份验证，不能把供应方删减过的网表查询当作检查证据。
