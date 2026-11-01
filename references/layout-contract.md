# 功能归属与端口交接

用于多核心并发、整体重排或长期任务。它是一份小型结构化交接记录，不冻结核心坐标、外框或最小间距；一次字段移动无需新建。沿用任务已有记录，只有需要机器核查时才保存为 JSON。

每个实体代表一个核心或支持组。实装器件单元只归属一个实体；支持组通过 `serves` 指向核心。关键保护、反馈或浮动供电支路还用 `serves_pins` 指向具体服务引脚/引脚对，不能只记录一个宽泛功能区名。重复层级或同位号多单元必须能消除歧义。`read_chains` 保存阅读链，`keep_together` 保存不能随意拆散的局部成员；同一封装不是所有单元必须靠拢的理由。已有物理限制放入 `physical_constraints`，不从原理图距离推导 PCB 摆放，也不自行发明物理约束。

直接连线的跨块端口，交付真实引脚端点、局部原点、出线方向与所用网格。标签端口用标签 UUID 定位。`direction` 是双方协商的可调整出线方向，不是库引脚旋转角；主 agent 可以因整体阅读关系要求回改。图形本体边缘不等于引脚端点；特殊库网格应如实声明，不强制吸附到默认网格。

## 最小示例

```json
{
  "version": 1,
  "scope": "listed",
  "cores": [
    {"id": "sense", "members": [{"ref": "U5", "unit": 1}]},
    {"id": "mcu", "members": [{"ref": "U1", "unit": 1}]}
  ],
  "support_groups": [{
    "id": "adc_filter",
    "members": [{"ref": "R108"}, {"ref": "C105"}],
    "serves": "mcu",
    "serves_pins": [{"ref": "U1", "unit": 1, "pin": "14"}],
    "physical_constraints": ["保留现有 ADC 输入滤波归属；原理图坐标不指定 PCB 距离"]
  }],
  "read_chains": [["sense", "adc_filter", "mcu"]],
  "keep_together": [{
    "id": "adc_rc",
    "members": [{"ref": "R108"}, {"ref": "C105"}]
  }],
  "ports": [{
    "id": "sense_out",
    "owner": "sense",
    "anchor": {"ref": "U5", "unit": 1, "pin": "1"},
    "net": "IL_SENSE",
    "role": "output",
    "connection": "wire",
    "direction": "right",
    "origin_mm": ["40.64", "40.64"],
    "local_mm": ["12.70", "0"],
    "grid_mm": "1.27"
  }]
}
```

示例位号、引脚与坐标仅示意，应替换为本次源文件的实际对象。`scope: "listed"` 只检查列出的单元；`"sheet"` 要求当前根页所有非电源单元都已归属。`cores` 必填，其他列表可省略。成员选择器为 `ref` 加可选的 `unit`；省略 unit 只有唯一匹配时成立。`sheet_path` 预留完整实例路径，但本版工具仅支持 `/` 根页，非根页返回未覆盖，不猜测。

端口 `anchor` 二选一：`{"ref":"U5","unit":1,"pin":"1"}` 或 `{"uuid":"当前标签的 UUID"}`。`origin_mm + local_mm` 必须等于当前实际锚点。`grid_mm` 可省略；提供时只验证相对于该原点的声明网格。角色枚举为 `input/output/bidirectional/tri_state/passive`，连接方式为 `wire/label`，方向为 `left/right/up/down`。标签锚点可核对实际文字和标签类型；引脚端口的网名与供消角色由负责人核实。

`keep_together` 跨不同核心时写明 `cross_owner_reason`；属于同一核心的支持组不算跨核心。它保存组合意图，不把成员当刚性大矩形，不自动要求所有网络直接接线。约束需要变更时先核对原意，再有依据地更新记录；不能改记录来掩盖支持组放错位置。

### 引脚对归属示例

以用户指出的 D3 栅源保护为例：若当前库已核实 Q9 的 G=1、S=2，且 Q9 已属于 `sense_switch` 核心，可在对应列表中使用以下片段。换器件时重新核实引脚，不照抄编号。

```json
{
  "support_groups": [{
    "id": "gate_clamp",
    "members": [{"ref": "D3"}],
    "serves": "sense_switch",
    "serves_pins": [
      {"ref": "Q9", "unit": 1, "pin": "1"},
      {"ref": "Q9", "unit": 1, "pin": "2"}
    ]
  }],
  "keep_together": [{
    "id": "protected_mos",
    "members": [{"ref": "Q9", "unit": 1}, {"ref": "D3"}]
  }]
}
```

这一记录保留 Q9/D3 的保护关系，而不仅是两个同名网络。工具只检查成员/端点存在与一致性，不会因记录了 keep_together 就证明摆放正确；实际构图按布局与绘图细则查看，不使用未经校准的固定距离阈值代替判断。

## 检查与边界

```powershell
python -B -X utf8 scripts/layout_contract.py layout.json main.kicad_sch --output layout-check.json
```

从 skill 目录运行，或替换为实际脚本路径。脚本只读两个输入，输出源与约定内容的 SHA256。退出码 `0` 为声明范围内一致，`1` 为确定差异，`2` 为未覆盖/输入错误；存在确定差异时总状态优先为 `DIFFERENT`，仍需读取全部 `findings` 中的未覆盖项。

它检查实体/成员引用、重复归属、指定全页时的遗漏单元、支持对象、阅读链引用、组合成员和端口锚点/声明网格。引脚几何复用探针经原生校核的正交变换。库未展开、引脚不唯一或层级未支持时报告 `UNVERIFIED`，不修线或改库。

`PASS` 只表示记录和所覆盖源对象一致：不能证明服务对象选对、阅读顺序合理、局部紧凑、PCB 回路面积或实际网络正确。`direction`、物理约束文本和组合意图属于交接声明；引脚端口仍须结合原生网表核对网络/供消角色。电气检查使用验证细则的快照/比较流程，图面仍按当前原生渲染审查。层级任务可逐实际页检查局部记录，但不能据根页 PASS 宣称全工程归属已验证。
