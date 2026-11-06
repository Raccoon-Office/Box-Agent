# Box-Agent Session Log 格式与恢复规则

本文供读取、调试或扩展 Session Log 的同事使用，逐项说明每条 JSONL 记录的格式、写入含义及恢复行为。固定源码为 PR #250 `9f0db4746fd9fba08adbe5580811bab946b6f069`，与[ACP 协议总表](ACP_PROTOCOL_CATALOG_MAIN_CN.md)一致；不包含工作区未提交的切云代码，也不声明该提交已经合入 main。

Session Log 是会话持久事实源，不是 ACP 消息录屏，也不是每次模型请求的完整副本。物理日志、有效历史和模型上下文必须分开读取。

## 1. 存放位置与读取视图

默认目录为 `~/.box-agent/sessions/<sha256(session_id)>/session.jsonl`；配置 `BOX_AGENT_HOME` 后为该 profile 下的 `sessions`，调用者也可显式提供 root。目录哈希使用日志逻辑 session_id，不是 ACP `acp-v1-*` 句柄。ACP 有产品 ID 时使用产品 ID 建日志；子代理有自己的日志、父身份及深度。

同目录还有 `.writer.lock`、`images/<sha256>.png或jpg`、`skill-references/<sha256>.txt`；损坏恢复可生成 `session.recovery-<uuid>.jsonl`。普通上传文件的正文不因 resource_link 自动复制到 Session Log；这里保存文件关联和版本。图片及技能快照使用独立文件引用。

| 视图 | 读取方式 | 含义 |
|---|---|---|
| 物理记录 | 头部＋全部 JSONL 行 | 含被编辑撤销的旧未来、压缩前记录、片段和诊断；seq 永不因编辑重新编号 |
| 有效事件时间线 | `_active_events()` → `effective_events()` | 应用 history/revise，排除已撤销未来 |
| 当前模型历史 Surface | `replay().messages` | 对有效事件应用 append/replace/reset，恢复当前压缩后的消息；不是用户完整展示历史 |
| 原始有效展示历史 | `history_messages()` | 排除压缩区间消息及 surfaceDerived 副本，应用修订及 reset；用于 ACP load |
| 单次模型视图 | ContextEngine / PreparedContext | 在持久事实之上派生资料、策略、图片及预算；不改写原始输入 |

## 2. 每行公共格式

UTF-8 JSONL，一行一个对象，每行末尾 `\n`，禁止 NaN。写入使用紧凑 JSON；下方为便于阅读缩进。时间 `createdAt/time` 是 Unix 毫秒整数。首行是 session 头部，不带 seq；后续 seq 从0开始连续递增。append 写入进程文件缓冲，flush 才执行 flush＋fsync；不能把 append 返回或 events 内存列表当作已完成持久化证明。

### 2.1 首行 session

| 字段 | 类型 | 含义 |
|---|---|---|
| type | 固定 session | 不是普通事件 |
| version | int，当前1 | 读取器版本约束 |
| id | 非空 string | 逻辑会话身份 |
| createdAt | int | 创建时间 |
| cwd | string | normcase(abspath) 归一化工作区；不解析符号链接身份，恢复必须匹配 |
| parentSession | string，可选 | 子会话的父逻辑会话ID |
| origin | string，可选 | 例如 subagent |
| delegationDepth | int，可选 | 创建时非零才写入 |
| recoverySource | string，可选 | 损坏恢复原始备份的同目录文件名 |

```json
{
  "type": "session",
  "version": 1,
  "id": "chat-a",
  "createdAt": 1793930400000,
  "cwd": "E:\\work\\demo"
}
```


### 2.2 后续事件外壳

| 字段 | 类型 | 含义 |
|---|---|---|
| type | 非空 string | 事件名称，见下一节 |
| seq | int | 物理事件序号，从0开始 |
| time | int | 写入时刻 |
| data | object | 事件内容；user/message 的 data 本身就是消息，不再嵌套 message |
| ignorable | boolean，可选 | 仅写 true；未知类型可忽略并保留物理记录，否则读取拒绝 |
| surfaceOp | append 或对象，可选 | 仅有效消息事件参与 Surface：append 或 `{op:"replace",start:<seq>,end:<seq>}` |
| sourceEventSeqs | int[]，可选 | 派生/恢复记录的来源序号，不等于替换范围或父会话ID |

replace 的 start/end 是**当前 Surface 中存在的消息节点 seq，首尾都包含**，不是数组下标，也不是任意物理事件范围。所有替换都追加新行，不改原文件。替换区间不存在或反向会使 replay 拒绝。

## 3. 事件逐项格式

共 **25 种已登记事件＋1种可忽略诊断扩展**。下面每个 JSON 是独立的记录形状示例，seq:0 不可直接串成同一个日志；指纹/摘要/资源路径是演示值，真实输入必须由生成器计算并有对应文件。字段约束区分生产者写入形状与恢复读取器：append 不是逐类型 JSON Schema 校验器，开放 result/arguments 也不是封闭枚举。

| 类型 | data 字段 | 恢复/业务意义 |
|---|---|---|
| `turn/start` | turn:int | Agent 一次运行开始；turn 是日志运行序号，不是 ACP turnId。 |
| `turn/binding` | turn_id:string,prompt_fingerprint:string,input_fingerprint?:string | ACP 接受轮次前预留业务身份；旧记录可无 input_fingerprint。绑定存在不等于用户输入已提交。 |
| `history/revise` | revision_id,previous_revision_id,target_turn_id,turn_id,prompt_fingerprint:string; restore_through_seq:int | 恢复到有效时间线上的指定 seq，撤销其后旧未来；物理日志不删除。cutoff=-1 表示清空有效前缀。 |
| `turn/end` | turn:int,reason:{kind:string,message?:string} | 标记运行终结，不等于用户目标成功。主 Agent：end_turn→completed；cancelled→aborted+message:cancelled；其他见下表。 |
| `step/start` | turn:int,step:int | 一次模型调用及工具执行周期开始。 |
| `step/end` | turn:int,step:int | 步骤完成或恢复补闭；无最终正文。 |
| `user/message` | data 本身为 Message；额外 source、images?、surfaceDerived? | 有效用户/运行时消息；surfaceOp 必须明确。input_facts 承载原始输入及附件关联，不能把 input/context 当其替代。 |
| `input/contract` | schemaVersion:int，当前1 | 首次保存需要新输入合同的消息前写入；不可忽略，旧版本遇到未知必需类型应拒绝。 |
| `input/adopted` | id:string,fingerprint:string,files:FileFact[] | 请求提交回调复核版本后写入，表示该输入已被请求采用；不证明模型有效响应或工具已读取。 重启后的响应完成判定及物理事件扫描限制见4.2.1。 |
| `assistant/chunk` | turn:int,step:int或null,kind:text/thinking,content:string | 流式审计片段，不加入 replay().messages，也不能与完整 assistant/message 再次拼接。 |
| `assistant/message` | turn:int,step:int或null,message:Message; modelRound?,contextUsage?,surfaceDerived? | 完整助手消息/工具调用声明；参与 Surface 重建。modelRound 表示已接受模型响应轮次。 |
| `tool/call` | turn:int,step:int,callId:string,name:string,arguments:object | 持久派发事实；串行立即 flush，批量并行先全部记录并 flush 再启动。不证明副作用完成。 |
| `tool/result` | turn:int,step:int或null,message:Message; result?,error?,images?,surfaceDerived? | 有效工具回复参与 Surface；正常 result 是结构结果，恢复补闭可只有 error。 |
| `request/header` | turn:int,step:int,header:{config,system,tools} | 模型请求配置、系统文字和工具 Schema 快照；不是完整 provider 请求体，常规历史另由 Surface/投影提供。 |
| `request/context` | 普通：turn,step,provider,model,tokenLimit; autoMemoryContext?,skillReferences?,imageContext?,inputContext?；图片专用分支可仅 imageContext | 请求关联的候选/准备事实；存在不证明模型已收到。summary、continuation、驱逐等有不同形状，见嵌套字段。 |
| `session/end-seed` | 空对象 | prepare_resume 修复未闭合生命周期后写入的恢复边界；不是关闭会话，也不是正常 task 完成。 |
| `surface/reset` | reason:string | 清空有效 Surface 和图片投影，不删除物理历史；replay 不在该分支清空 goal/plan/todos/skills。 |
| `goal/change` | goal:Goal或null | 以最后一份快照替换目标，null 清空。 |
| `plan/write` | plan:Plan或null | 以最后一份快照替换计划，null 清空；不同于 ACP rawOutput.plan_snapshot 外壳。 |
| `todo/write` | todos:Todo[] | 全量替换待办列表，[] 清空；恢复再由 TodoStore 校验。 |
| `skill/change` | skills:SkillRead[] | 技能读取状态全量快照，[] 清空；历史哈希是来源证据，恢复还需技能来源验证。 |
| `compaction/start` | turn,step,estimatedBefore,tokenLimit:int | 准备压缩；先保存当前未写入消息并 flush。 |
| `compaction/summary` | turn,step,mode:summary/fallback,message:Message,estimatedBefore,estimatedAfter:int,error:string或null | 压缩摘要审计记录；真正模型 Surface 替换通过随后的消息 surfaceOp 完成，不能直接追加此摘要。 |
| `compaction/end` | turn,step:int,mode:summary/fallback,error:string或null | 结束压缩记录区间；history_messages 排除区间内的压缩消息副本。 |
| `subagent/descriptor` | version:1,mode:"one-shot",provider:"local",label?:string | 写在子会话日志中，描述子代理身份；关联父会话的是头部 parentSession。 |
| `input/context` | turn_id:string或空值；展开规范化 input_context/selection_context/database_context/files 等 | 当前 ACP 的可忽略诊断扩展（ignorable:true）；不在 KNOWN_EVENT_TYPES，不负责恢复有效输入。 |

### 3.1 turn/start

Agent 一次运行开始；turn 是日志运行序号，不是 ACP turnId。

字段：turn:int。

```json
{
  "type": "turn/start",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1
  }
}
```

写入位置：[box_agent/agent.py:1314](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1314)；[box_agent/tools/sub_agent_tool.py:281](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L281)。

### 3.2 turn/binding

ACP 接受轮次前预留业务身份；旧记录可无 input_fingerprint。绑定存在不等于用户输入已提交。

字段：turn_id:string,prompt_fingerprint:string,input_fingerprint?:string。

```json
{
  "type": "turn/binding",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn_id": "t1",
    "prompt_fingerprint": "50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c",
    "input_fingerprint": "51a421b2f9a96e8f0f58b49848114df3196f8120be4f95257cc2535319eeec71"
  }
}
```

写入位置：[box_agent/acp/__init__.py:2661](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2661)。

### 3.3 history/revise

恢复到有效时间线上的指定 seq，撤销其后旧未来；物理日志不删除。cutoff=-1 表示清空有效前缀。

字段：revision_id,previous_revision_id,target_turn_id,turn_id,prompt_fingerprint:string; restore_through_seq:int。

```json
{
  "type": "history/revise",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "revision_id": "t2",
    "previous_revision_id": "initial",
    "restore_through_seq": -1,
    "target_turn_id": "t1",
    "turn_id": "t2",
    "prompt_fingerprint": "50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c"
  }
}
```

写入位置：[box_agent/session_history.py:114](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_history.py#L114)。

### 3.4 turn/end

标记运行终结，不等于用户目标成功。主 Agent：end_turn→completed；cancelled→aborted+message:cancelled；其他见下表。

字段：turn:int,reason:{kind:string,message?:string}。

```json
{
  "type": "turn/end",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "reason": {
      "kind": "completed"
    }
  }
}
```

写入位置：[box_agent/agent.py:1539](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1539)；[box_agent/agent.py:1478](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1478)；[box_agent/session_log.py:1350](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1350)；[box_agent/tools/sub_agent_tool.py:1070](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1070)；[box_agent/tools/sub_agent_tool.py:935](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L935)。

### 3.5 step/start

一次模型调用及工具执行周期开始。

字段：turn:int,step:int。

```json
{
  "type": "step/start",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1
  }
}
```

写入位置：[box_agent/agent.py:1440](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1440)；[box_agent/tools/sub_agent_tool.py:1061](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1061)；[box_agent/tools/sub_agent_tool.py:890](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L890)。

### 3.6 step/end

步骤完成或恢复补闭；无最终正文。

字段：turn:int,step:int。

```json
{
  "type": "step/end",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1
  }
}
```

写入位置：[box_agent/agent.py:1535](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1535)；[box_agent/agent.py:1451](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1451)；[box_agent/agent.py:1473](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1473)；[box_agent/session_log.py:1344](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1344)；[box_agent/tools/sub_agent_tool.py:1069](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1069)；[box_agent/tools/sub_agent_tool.py:902](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L902)；[box_agent/tools/sub_agent_tool.py:930](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L930)。

### 3.7 user/message

有效用户/运行时消息；surfaceOp 必须明确。input_facts 承载原始输入及附件关联，不能把 input/context 当其替代。

字段：data 本身为 Message；额外 source、images?、surfaceDerived?。

```json
{
  "type": "user/message",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "role": "user",
    "content": "请分析报表。",
    "source": "user",
    "request_only_input_tokens": 0,
    "input_facts": {
      "schemaVersion": 1,
      "id": "t1",
      "fingerprint": "51a421b2f9a96e8f0f58b49848114df3196f8120be4f95257cc2535319eeec71",
      "blocks": [
        {
          "type": "text",
          "text": "请分析报表。"
        }
      ],
      "context": {},
      "workspace": "E:\\work\\demo",
      "policy": {},
      "resourceImages": {}
    }
  },
  "surfaceOp": "append"
}
```

写入位置：[box_agent/session_log.py:996](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L996)；[box_agent/session_log.py:899](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L899)。

### 3.8 input/contract

首次保存需要新输入合同的消息前写入；不可忽略，旧版本遇到未知必需类型应拒绝。

字段：schemaVersion:int，当前1。

```json
{
  "type": "input/contract",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "schemaVersion": 1
  }
}
```

写入位置：[box_agent/session_log.py:727](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L727)。

### 3.9 input/adopted

请求提交回调复核版本后写入，表示该输入已被请求采用；不证明模型有效响应或工具已读取。 重启后的响应完成判定及物理事件扫描限制见4.2.1。

字段：id:string,fingerprint:string,files:FileFact[]。

```json
{
  "type": "input/adopted",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "id": "t1",
    "fingerprint": "51a421b2f9a96e8f0f58b49848114df3196f8120be4f95257cc2535319eeec71",
    "files": []
  }
}
```

写入位置：[box_agent/context_input.py:395](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L395)。

### 3.10 assistant/chunk

流式审计片段，不加入 replay().messages，也不能与完整 assistant/message 再次拼接。

字段：turn:int,step:int或null,kind:text/thinking,content:string。

```json
{
  "type": "assistant/chunk",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "kind": "text",
    "content": "已完成"
  }
}
```

写入位置：[box_agent/agent.py:1421](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1421)；[box_agent/tools/sub_agent_tool.py:875](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L875)。

### 3.11 assistant/message

完整助手消息/工具调用声明；参与 Surface 重建。modelRound 表示已接受模型响应轮次。

字段：turn:int,step:int或null,message:Message; modelRound?,contextUsage?,surfaceDerived?。

```json
{
  "type": "assistant/message",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "message": {
      "role": "assistant",
      "content": "已完成分析。",
      "request_only_input_tokens": 0
    },
    "modelRound": 1
  },
  "surfaceOp": "append"
}
```

写入位置：[box_agent/session_log.py:1004](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1004)；[box_agent/session_log.py:908](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L908)。

### 3.12 tool/call

持久派发事实；串行立即 flush，批量并行先全部记录并 flush 再启动。不证明副作用完成。

字段：turn:int,step:int,callId:string,name:string,arguments:object。

```json
{
  "type": "tool/call",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "callId": "call-1",
    "name": "read_file",
    "arguments": {
      "path": "sales.csv"
    }
  }
}
```

写入位置：[box_agent/kernel/tool_messages.py:32](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/tool_messages.py#L32)。

### 3.13 tool/result

有效工具回复参与 Surface；正常 result 是结构结果，恢复补闭可只有 error。

字段：turn:int,step:int或null,message:Message; result?,error?,images?,surfaceDerived?。

```json
{
  "type": "tool/result",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "message": {
      "role": "tool",
      "content": "a,b\n1,2",
      "tool_call_id": "call-1",
      "name": "read_file",
      "request_only_input_tokens": 0
    },
    "result": {
      "success": true,
      "content": "a,b\n1,2",
      "error": null,
      "rawOutput": null,
      "policyDecision": null
    }
  },
  "surfaceOp": "append"
}
```

写入位置：[box_agent/session_log.py:1012](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1012)；[box_agent/session_log.py:1316](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1316)；[box_agent/session_log.py:928](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L928)。

### 3.14 request/header

模型请求配置、系统文字和工具 Schema 快照；不是完整 provider 请求体，常规历史另由 Surface/投影提供。

字段：turn:int,step:int,header:{config,system,tools}。

```json
{
  "type": "request/header",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "header": {
      "config": {
        "provider": "openai",
        "model": "example-model",
        "maxOutputTokens": 4096
      },
      "system": "你是助手。",
      "tools": []
    }
  }
}
```

写入位置：[box_agent/kernel/loop.py:1631](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1631)；[box_agent/tools/sub_agent_tool.py:1246](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1246)。

### 3.15 request/context

请求关联的候选/准备事实；存在不证明模型已收到。summary、continuation、驱逐等有不同形状，见嵌套字段。

字段：普通：turn,step,provider,model,tokenLimit; autoMemoryContext?,skillReferences?,imageContext?,inputContext?；图片专用分支可仅 imageContext。

```json
{
  "type": "request/context",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 1,
    "provider": "openai",
    "model": "example-model",
    "tokenLimit": 32000,
    "inputContext": {
      "phase": "candidate",
      "inputs": [
        {
          "id": "t1",
          "fingerprint": "51a421b2f9a96e8f0f58b49848114df3196f8120be4f95257cc2535319eeec71",
          "files": []
        }
      ]
    }
  }
}
```

写入位置：[box_agent/agent.py:1023](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1023)；[box_agent/context_input.py:122](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L122)；[box_agent/image_context.py:289](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/image_context.py#L289)；[box_agent/kernel/loop.py:1676](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1676)；[box_agent/tools/sub_agent_tool.py:1261](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1261)。

### 3.16 session/end-seed

prepare_resume 修复未闭合生命周期后写入的恢复边界；不是关闭会话，也不是正常 task 完成。

字段：空对象。

```json
{
  "type": "session/end-seed",
  "seq": 0,
  "time": 1793930400000,
  "data": {}
}
```

写入位置：[box_agent/session_log.py:1361](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1361)。

### 3.17 surface/reset

清空有效 Surface 和图片投影，不删除物理历史；replay 不在该分支清空 goal/plan/todos/skills。

字段：reason:string。

```json
{
  "type": "surface/reset",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "reason": "agent.clear_history"
  }
}
```

写入位置：[box_agent/agent.py:1196](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1196)；[box_agent/session_log.py:1068](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1068)。

### 3.18 goal/change

以最后一份快照替换目标，null 清空。

字段：goal:Goal或null。

```json
{
  "type": "goal/change",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "goal": {
      "objective": "完成报表",
      "status": "active",
      "createdAt": "2026-11-06T10:00:00",
      "updatedAt": "2026-11-06T10:00:00",
      "evidence": [],
      "progress": [],
      "blockedReason": null,
      "completedBy": null
    }
  }
}
```

写入位置：[box_agent/agent.py:717](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L717)；[box_agent/cli.py:980](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/cli.py#L980)；[box_agent/cli.py:2216](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/cli.py#L2216)。

### 3.19 plan/write

以最后一份快照替换计划，null 清空；不同于 ACP rawOutput.plan_snapshot 外壳。

字段：plan:Plan或null。

```json
{
  "type": "plan/write",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "plan": {
      "id": "1",
      "title": "报表分析",
      "objective": "核对销售额",
      "scope": "sales.csv",
      "status": "active",
      "steps": [
        {
          "id": "1",
          "title": "读取数据",
          "details": "核对列名"
        }
      ],
      "verification": [
        "复核总额"
      ],
      "risks": [],
      "assumptions": [],
      "created_at": "2026-11-06T10:00:00",
      "updated_at": "2026-11-06T10:00:00"
    }
  }
}
```

写入位置：[box_agent/tools/plan_tool.py:199](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L199)。

### 3.20 todo/write

全量替换待办列表，[] 清空；恢复再由 TodoStore 校验。

字段：todos:Todo[]。

```json
{
  "type": "todo/write",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "todos": [
      {
        "id": "1",
        "task": "核对报表",
        "status": "in_progress",
        "priority": "medium",
        "created_at": "2026-11-06T10:00:00"
      }
    ]
  }
}
```

写入位置：[box_agent/tools/todo_tool.py:538](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L538)。

### 3.21 skill/change

技能读取状态全量快照，[] 清空；历史哈希是来源证据，恢复还需技能来源验证。

字段：skills:SkillRead[]。

```json
{
  "type": "skill/change",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "skills": [
      {
        "name": "spreadsheet",
        "sha256": "50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c",
        "loadOrder": 1,
        "source": "builtin",
        "path": "E:\\skills\\spreadsheet\\SKILL.md",
        "reason": "tool",
        "deliveredRanges": [
          [
            0,
            100
          ]
        ],
        "deliveredComplete": false
      }
    ]
  }
}
```

写入位置：[box_agent/agent.py:841](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L841)；[box_agent/skill_runtime.py:246](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_runtime.py#L246)；[box_agent/skill_runtime.py:395](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_runtime.py#L395)。

### 3.22 compaction/start

准备压缩；先保存当前未写入消息并 flush。

字段：turn,step,estimatedBefore,tokenLimit:int。

```json
{
  "type": "compaction/start",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 2,
    "estimatedBefore": 35000,
    "tokenLimit": 32000
  }
}
```

写入位置：[box_agent/kernel/context_engine.py:829](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L829)；[box_agent/kernel/loop.py:1123](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1123)。

### 3.23 compaction/summary

压缩摘要审计记录；真正模型 Surface 替换通过随后的消息 surfaceOp 完成，不能直接追加此摘要。

字段：turn,step,mode:summary/fallback,message:Message,estimatedBefore,estimatedAfter:int,error:string或null。

```json
{
  "type": "compaction/summary",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 2,
    "mode": "summary",
    "message": {
      "role": "user",
      "content": "历史摘要",
      "request_only_input_tokens": 0
    },
    "estimatedBefore": 35000,
    "estimatedAfter": 8000,
    "error": null
  }
}
```

写入位置：[box_agent/kernel/loop.py:1182](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1182)。

### 3.24 compaction/end

结束压缩记录区间；history_messages 排除区间内的压缩消息副本。

字段：turn,step:int,mode:summary/fallback,error:string或null。

```json
{
  "type": "compaction/end",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn": 1,
    "step": 2,
    "mode": "summary",
    "error": null
  }
}
```

写入位置：[box_agent/kernel/loop.py:1202](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1202)。

### 3.25 subagent/descriptor

写在子会话日志中，描述子代理身份；关联父会话的是头部 parentSession。

字段：version:1,mode:"one-shot",provider:"local",label?:string。

```json
{
  "type": "subagent/descriptor",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "version": 1,
    "mode": "one-shot",
    "provider": "local",
    "label": "分析子任务"
  }
}
```

写入位置：[box_agent/tools/sub_agent_tool.py:290](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L290)。

### 3.26 input/context

当前 ACP 的可忽略诊断扩展（ignorable:true）；不在 KNOWN_EVENT_TYPES，不负责恢复有效输入。

字段：turn_id:string或空值；展开规范化 input_context/selection_context/database_context/files 等。

```json
{
  "type": "input/context",
  "seq": 0,
  "time": 1793930400000,
  "data": {
    "turn_id": "t1",
    "input_context": {
      "schemaVersion": 1,
      "userRequest": "请分析报表。"
    }
  },
  "ignorable": true
}
```

写入位置：[box_agent/acp/__init__.py:2668](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2668)。

## 4. 消息及附加字段

### 4.1 Message

普通 user 的 `data`、assistant/tool 的 `data.message` 使用同一个 Message 序列化；默认值 request_only_input_tokens:0 会保留，None 字段省略。

| 字段 | 类型及适用范围 |
|---|---|
| role | string；Surface 接受 user/assistant/tool，system 通常进入 request/header.system |
| content | string 或内容块 object[]；新结构化用户消息保存原文字串，有序附件另在 input_facts |
| source | user/runtime；模型对象默认排除序列化，user/message 写入时显式补回 |
| thinking | 可选 string，助手思考内容 |
| tool_calls | 可选数组，每项 `{id,type:"function",function:{name,arguments}}`；arguments 为已解析的参数对象（dict[str, Any]），不能传 JSON 字符串 |
| tool_call_id / name | 可选 string，tool 回复的调用ID/工具名 |
| usage | 可选 `{prompt_tokens,completion_tokens,total_tokens}`；内部输入/缓存字段不直接序列化 |
| request_only_input_tokens | 非负 int，默认0；恢复后估算上下文所需的请求独有输入调整 |
| host_connector_status | 可选 string，可信宿主状态快照，不从用户XML猜测 |
| input_facts | 可选 object；旧消息可没有，见4.2 |

带工具调用的完整消息记录如下。function.arguments 与 tool/call.data.arguments 都是对象；把它写成字符串 `"{\"path\":\"sales.csv\"}"` 会使 Message 校验失败。该助手消息声明调用，实际派发和结果仍需各自记录。

```json
{
  "type": "assistant/message",
  "seq": 3,
  "time": 1793930400003,
  "data": {
    "turn": 1,
    "step": 1,
    "message": {
      "role": "assistant",
      "content": "先读取报表。",
      "tool_calls": [
        {
          "id": "call-1",
          "type": "function",
          "function": {
            "name": "read_file",
            "arguments": {
              "path": "sales.csv"
            }
          }
        }
      ],
      "request_only_input_tokens": 0
    }
  },
  "surfaceOp": "append"
}
```

`trace_redact_content` 不落盘。事件级 `images`、`modelRound`、`contextUsage`、`surfaceDerived` 不属于 Message 正文字段：images 关联用户/工具图片；modelRound 与已接受助手响应同写；surfaceDerived:true 标记替换时保留的原消息副本，展示历史应排除。

### 4.2 input_facts 与文件关联

| 字段 | 类型与含义 |
|---|---|
| schemaVersion | 当前1 |
| id | 原始输入身份；普通输入可来自 turn_id，补充来自 injectionId |
| fingerprint | SHA-256；规范化原始 blocks＋语义 context 的指纹，不使用今天磁盘内容重算请求身份 |
| blocks | 原始顺序的 text/resource_link/image；内联 image 保存 `{type:"image",mimeType,sha256,imageIndex}`，不保存 data |
| context | 规范化 input_context/selection_context/database_context 及 files |
| workspace | 准备输入时的绝对工作区 |
| policy | 例如 ui_language、user_decision_response、turn_hints；普通直接 API 可为空 |
| resourceImages | 原始图片 link URI → images 中对应的零基索引 |
| kind | 补充时 user_supplement；普通输入通常没有 |
| images | 可选 ImageRef[]，与本条输入绑定的持久图片引用 |
| image_result | 可选 string，图片准备的模型侧证据及指导，不是用户原文 |
| image_tool | 可选 `{toolCallId,name,arguments,success,content}`，回放图片准备结果所需事实 |

内联块的 imageIndex 是 **images 数组的零基索引**；ImageRef 本身的 imageIndex 是 register 的一基图片序号，二者不能混用。file fact 不是用户上传文件正文，也不授予读取权限：

```json
{
  "name": "sales.csv",
  "path": "E:\\work\\demo\\sales.csv",
  "uri": "file:///E:/work/demo/sales.csv",
  "size": 7,
  "mimeType": "text/csv",
  "version": {
    "algorithm": "sha256",
    "digest": "50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c"
  }
}
```


普通文件会流式计算摘要，不形成文件内容快照。`input/adopted` 引用这份事实；正常链路在有效响应前复核版本；重启后的完成判定存在4.2.1所述物理事件扫描限制。之后工具仍看到共享工作区的当前文件。同一文件可被不同会话修改，但附件与消息的关联仍按会话保存。

input/contract 的写入条件：input_facts 有非空 context/policy/kind，或包含非 text 块。仅纯文字、无附加上下文的记录可以没有该特性标记；不能仅凭 schemaVersion=1 推断所有旧运行包都能读新日志。

### 4.2.1 重新配置运行时的采用状态恢复及限制

`box_agent/context_input.py` 的 `DefaultContextEngine.configure_run()` 每次重置 `_input_adoptions` 和 `_input_settled`，随后按顺序扫描 `session_store.events`：

1. 遇到 input/adopted，将 data.id 加入 `_input_adoptions`。
2. 遇到非 surfaceDerived 的 assistant/message，将此前所有已采用 ID 加入 `_input_settled`。不要求它是最终回答；带 tool_calls 的有效助手消息也符合条件。该判断没有逐输入请求关联，也不在这里核对响应的 modelRound。
3. 后续准备请求时，只有仍在输入记录中且 ID 不属于 `_input_settled` 的输入作为 pending，继续复核文件版本；已 settled 的输入跳过这项复核。采用记录后没有符合条件的助手消息时，不会仅因 adopted 就认定已经响应。

**当前限制：此处扫描全部物理 events，而非 `_active_events()` 的修订后时间线。** history/revise 已撤销的 input/adopted 和 assistant/message 仍可能进入这两个集合。例如 seq 0 采用输入 i-1，seq 1 写助手响应，seq 2 的 history/revise 恢复到 -1；有效时间线已不含前两条，但 configure_run 仍将 i-1 视为 adopted 和 settled。若待处理输入使用同一 ID，版本复核可能被跳过。不能把 replay 的有效历史隔离规则推断到这里。

这描述的是 9f0db474 的实际行为，不是预期合同。是否改为扫描有效事件、如何处理现有采用记录及恢复兼容性，需要另行代码修复评估；本文不声明该问题已修复。

### 4.3 图片引用与交付状态

图片字节保存到同会话 images 目录；data.images / input_facts.images 保存引用，不放 Base64 正文。ImageRef 是 total=False 的开放形状：持久化失败的旧输入可能缺 blob 字段，不能把全部字段标成必填。

[box_agent/image_store.py:21](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/image_store.py#L21)

```python
class ImageRef(TypedDict, total=False):
    """JSON-safe occurrence metadata; unavailable images may lack blob fields."""

    imageId: str
    source: str
    contentRef: str
    sha256: str
    media_type: str
    width: int
    height: int
    source_bytes: int
    base64Bytes: int
    createdRound: int
    messageId: str
    messageSeq: int
    toolCallId: str
    toolName: str
    imageIndex: int
    sourceLabel: str
    eligible: bool
    active: bool
    evicted: bool
    evictionReason: str
    delivered: bool
    noticeDelivered: bool
    sourceSessionId: str
    sourceMessageSeq: int
    sourceUserMessageSeq: int
    sourceToolSessionId: str
    sourceToolResultSeq: int
    sourceImageOrder: int
```


主要字段分组：

- blob：contentRef、sha256、media_type、width、height、source_bytes、base64Bytes；存储器另外返回 type:input_image。
- 发生身份：imageId、messageId、createdRound、imageIndex、source；可带 sourceLabel/toolCallId/toolName。
- 状态：eligible、active、evicted、evictionReason、delivered、noticeDelivered；replay 会附加 messageSeq，并根据有效助手响应更新 firstDeliveredRound/lastDeliveredRound。
- 导入来源：sourceSessionId/sourceMessageSeq/sourceUserMessageSeq/sourceToolSessionId/sourceToolResultSeq/sourceImageOrder。

request/context 中 selected 只表示为请求选中；与其 requestRound 对应的 assistant/message.modelRound 提交后，恢复才确认 delivered。purpose:summary 的图片请求不会按普通主模型响应标记交付。缺失/超预算与未支持视觉不能写成“已看过图片”。

已知实现缺陷：被取消的图片补充可能已进入运行中 ImageContext，但没有提交 user/message。不能凭“无持久输入”断言进程内模型绝不消费该图片；该缺陷与日志格式定义分开跟踪。

### 4.4 contextUsage

有效助手响应可在事件 data 上附加下列对象；用于恢复测得的上下文用量，不改变公开 usage 的三项计数。version、哈希、类型或总数校验失败时忽略这份可选元数据。

`{version:1,route:"sha256:<hex>",history:["<hex>"],tools:{工具名:"sha256:<hex>"},requestOnly:["sha256:<hex>"],contextTokens:int,outputTokens:int,sha256:"<hex>"}`。

contextTokens>0，0≤outputTokens≤contextTokens；sha256 对 message 的公开序列化及除 sha256 外的 contextUsage 做规范化哈希。恢复只保留最后一份有效 request snapshot。精确哈希算法见附录代码，不手拼校验值。

## 5. request/context 的多种形状

| 字段 / 分支 | 内容与边界 |
|---|---|
| 普通请求 | turn、step、provider、model、tokenLimit；provider/model 可为null |
| 子代理简化请求 | turn、step、provider、model，可无 tokenLimit |
| autoMemoryContext | `{sha256,chars}`，是摘要/长度，不是全文 |
| inputContext | `{phase:"candidate",inputs:[{id,fingerprint,files}]}`；后续 input/adopted 才是提交采用 |
| imageContext / 准备 | `{phase:"prepared",purpose:"main"或"summary",requestRound,selected,encodedBytes,evicted,excluded,requestSize?,notices?,noticeImageIds?}` |
| imageContext / 延续 | `{phase:"continuation",modelRoundBaseline}`；导入图片时恢复模型轮数基线 |
| 图片驱逐 / summary | 整个 data 可仅含 imageContext，没有 turn/step/tokenLimit；不能按普通请求必填字段拒绝 |
| skillReferences | 请求独有技能引用列表，存在时记录引用来源/范围及不可变快照；不是 skill/change 的完整状态替代 |

selected 的每项按存在性选择 imageId/contentRef/sha256/media_type/width/height/base64Bytes。evicted/excluded 记录 imageId/reason，驱逐还记录 neverDelivered:boolean；内置 requestSize 包含 original_body_bytes/body_bytes/max_body_bytes/budget_bytes/image_count/non_image_bytes/fits/transforms；OpenAI 可追加 token_parameter_reserve_bytes/required_body_bytes。provider 可扩展，不能把它当业务事件类型。notices 是提示字符串数组。

技能正文快照通常为 `{contentRef:"skill-references/<sha256>.txt",sha256}`；sidecar 写入或完整性校验失败可退回 `{inlineContent,sha256}`，随 request/context 持久化。不可将缺失 contentRef 等同于没有正文；技能正文引用还带 name/revision/message_index/offset/end_offset；状态提示引用带 kind:"status"/message_index。message_index 是单次请求消息下标，不是日志 seq。完整构造见 skill_context.py 的投影实现。

## 6. 业务状态与生命周期枚举

- goal 快照：objective/status/createdAt/updatedAt/evidence/progress/blockedReason/completedBy。replay 只恢复 dict 或 null；后续 GoalState 还会归一化，不应把日志读取器当作目标状态业务校验器。
- plan.status：draft/active/revised/complete；steps 每项 id/title/details，verification/risks/assumptions 为字符串数组；created_at/updated_at 为ISO字符串。
- todo.status：pending/in_progress/completed；priority：high/medium/low；id 是正整数的规范字符串，task 非空，created_at 必填。正常未完成列表要求恰好一个 in_progress；旧状态另有迁移逻辑。
- skill/change 保存读取来源及已交付范围。旧记录可能只有 name/sha256/loadOrder，恢复兼容路径不得把缺失的可选新字段视为日志损坏。

| 来源 | turn/end.reason.kind |
|---|---|
| 主 Agent 正常结束 | completed |
| 主 Agent 取消 | aborted，同时 message:cancelled |
| 主 Agent 其他停止 | error/interrupted/max_steps/max_tokens/waiting_for_user |
| 恢复补闭 | interrupted |
| 子代理流式分支 | end_turn→completed；其他保留 StopReason.value，因此取消可能是 cancelled |
| 子代理简化分支 | completed 或 error |

这些是内部日志值，不等同 ACP stopReason，也不能只用 kind 判断目标完成。step、turn 为数字，业务 turn_id 为字符串，模型 modelRound 为另一条计数轴。

## 7. 编辑、压缩和异常恢复

### 7.1 编辑与压缩

history/revise 的 restore_through_seq 必须是当前有效时间线中的旧 seq，或 -1；小于当前修订事件 seq。revision_id/previous_revision_id/turn_id 均为非空字符串。被撤销的未来仍在文件里，但不再进入 replay 的目标、计划、待办、技能或图片状态。这不涵盖 configure_run 的输入采用状态扫描，后者的当前限制见4.2.1。

compaction/summary 记录摘要，随后 replace_surface 追加 replacement 消息及 surfaceOp。history_messages 忽略 summary→end 区间的消息和 surfaceDerived 副本；replay 则按 surfaceOp 重建压缩后的上下文。不要用“所有 message 行直接相加”同时实现这两种视图。

### 7.2 打开、独占锁、尾行与损坏

`SessionLog.create()`、`open()`、`open_with_projection()` 均获取会话目录 `.writer.lock` 的**非阻塞独占锁**；`open_or_create()` 也经这些入口获取锁。该基线的名称是 open_with_projection，不是 open_projected。open 即使仅用于检查，也以 r+b 打开日志，并可能截断残尾，不是旁路只读 API。

锁在读取/创建日志前取得，成功后由 SessionLog 对象持有至 close()；打开失败的异常路径释放已获取的锁，进程终止后操作系统释放锁。close() 关闭日志及锁句柄，但不删除 .writer.lock；锁文件存在不代表当前有人持锁。Windows 使用 msvcrt 的非阻塞字节锁，其他系统使用 flock 的 LOCK_EX | LOCK_NB。

已有持有者时抛 SessionLogInUseError。**占用不是日志损坏，recover=True 也不能据此重建或替换日志**。调试程序不能用上述 API 打开正在运行的会话作旁路读取，更不能删除锁文件强开。需要一致的恢复视图时，先由持有者 flush 并关闭后再打开；直接读取运行中文件只能作为可能滞后的诊断快照，不能据此认定尾部异常或写回修复。


读取器验证头部身份/工作区/版本、逐条 seq 连续、非空 type、data 对象。未知类型且 ignorable 不为 true 时抛 SessionLogIncompatible；未知版本也拒绝。不会为每种 event data 执行完整 Schema 校验，消息及修订等在投影时另行校验。

**末尾没有换行的残余记录会被截掉并 fsync，即使 recover=False。** 已换行的非法JSON/消息是损坏，不当作普通残尾。recover=True 只对允许恢复的损坏备份原文件后建立带 recoverySource 的新日志；不吞掉工作区不匹配、未知必需合同或修订损坏。ACP load 使用 recover=False，因此不会用空日志替代损坏历史，但仍经过公共残尾处理。

### 7.3 中断工具与恢复边界

prepare_resume 扫描未闭合 turn/step，并为缺失工具回复追加普通 tool/result：

| 已有记录 | 补闭错误 | 含义 |
|---|---|---|
| assistant.tool_calls 有声明，但无 tool/call | ToolNotStartedError / TOOL_NOT_STARTED | 未持久派发，必要时可重试 |
| 已有 tool/call，但无 tool/result | ToolOutcomeUnknownError / TOOL_OUTCOME_UNKNOWN | 可能已有外部副作用，结果未知，必须先查外部状态 |

补闭回复的 surfaceOp 为 append；已派发时 sourceEventSeqs 指向 tool/call 的 seq。之后补 step/end、turn/end(reason.kind:interrupted)，最后写 session/end-seed 并 flush。恢复日志和回放界面不会重跑历史工具。

## 8. 完整顺序示例

下面是一份最小纯文字会话的完整 JSONL；不包含可选 ACP binding/输入事实，不表示所有入口都严格采用这一事件排列。user/message 可以先于 turn/start；工具调用还需助手调用声明、派发记录和回复。

```jsonl
{"type":"session","version":1,"id":"example-chat","createdAt":1793930400000,"cwd":"E:\\work\\demo"}
{"type":"user/message","seq":0,"time":1793930400000,"data":{"role":"user","content":"请分析报表。","source":"user","request_only_input_tokens":0},"surfaceOp":"append"}
{"type":"turn/start","seq":1,"time":1793930400001,"data":{"turn":1}}
{"type":"step/start","seq":2,"time":1793930400002,"data":{"turn":1,"step":1}}
{"type":"assistant/message","seq":3,"time":1793930400003,"data":{"turn":1,"step":1,"message":{"role":"assistant","content":"已完成分析。","request_only_input_tokens":0}},"surfaceOp":"append"}
{"type":"step/end","seq":4,"time":1793930400004,"data":{"turn":1,"step":1}}
{"type":"turn/end","seq":5,"time":1793930400005,"data":{"turn":1,"reason":{"kind":"completed"}}}
```

## 9. 源码核对与扩展方法

当前清单与固定提交的 KNOWN_EVENT_TYPES 对齐，另枚举所有内置 append/_append_message_event 的字面事件生产点。插件可以自行追加 ignorable 扩展，不能把未来插件任意 data 当作固定封闭全集。新增必需事件时同时更新读取支持、写入方、恢复逻辑和本表；新增可忽略诊断不得承担必须恢复的业务状态。

以下源码用于核对嵌套字段与特殊分支，均来自同一固定提交，不读取工作区修改。


### ToolCall

[box_agent/schema/schema.py:61](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/schema/schema.py#L61)

```python
class ToolCall(BaseModel):
    """Tool call structure."""

    id: str
    type: str  # "function"
    function: FunctionCall
```

### FunctionCall

[box_agent/schema/schema.py:54](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/schema/schema.py#L54)

```python
class FunctionCall(BaseModel):
    """Function call details."""

    name: str
    arguments: dict[str, Any]  # Function arguments as dict
```

### Message

[box_agent/schema/schema.py:122](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/schema/schema.py#L122)

```python
class Message(BaseModel):
    """Chat message."""

    role: str  # "system", "user", "assistant", "tool"
    source: Literal["user", "runtime"] = Field(default="user", exclude=True)
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role
    usage: TokenUsage | None = None
    # Estimated request-only input included in this response's provider usage
    # but intentionally absent from the next durable request. Persist the
    # numeric adjustment so context estimation remains correct after resume.
    request_only_input_tokens: int = Field(default=0, ge=0)
    # Current trusted host snapshot, even when unchanged and omitted from text.
    # Persist for change detection/recovery; never infer state from user XML.
    host_connector_status: str | None = None
    # Durable original input and resource associations; only ContextEngine
    # derives the provider view. Older messages have no such record.
    input_facts: dict[str, Any] | None = None
    # Request-only multimodal overlays may carry raw image bytes to a provider
    # without persisting them in durable traces. Excluded from serialization so
    # this remains an internal observability directive.
    trace_redact_content: bool = Field(default=False, exclude=True)
```

### _context_usage_metadata

[box_agent/session_log.py:151](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L151)

```python
def _context_usage_metadata(message: Message) -> dict[str, Any] | None:
    usage = message.usage
    snapshot = usage._request_snapshot if usage is not None else None
    if message.role != "assistant" or snapshot is None or usage.context_tokens <= 0:
        return None
    metadata = {
        "version": 1,
        "route": snapshot.route,
        "history": list(snapshot.history),
        "tools": dict(snapshot.tools),
        "requestOnly": list(snapshot.request_only),
        "contextTokens": usage.context_tokens,
        "outputTokens": usage.output_tokens or usage.completion_tokens,
    }
    metadata["sha256"] = _context_usage_hash(message, metadata)
    return metadata
```

### _restore_context_usage

[box_agent/session_log.py:169](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L169)

```python
def _restore_context_usage(message: Message, metadata: Any) -> None:
    """Restore optional measured context without changing public usage fields."""
    if message.role != "assistant" or message.usage is None or not isinstance(metadata, dict):
        return
    if type(metadata.get("version")) is not int or metadata["version"] != 1:
        return

    def is_hash(value: Any, prefix: str = "") -> bool:
        return isinstance(value, str) and re.fullmatch(prefix + r"[0-9a-f]{64}", value) is not None

    history, tools = metadata.get("history"), metadata.get("tools")
    request_only = metadata.get("requestOnly")
    context_tokens, output_tokens = metadata.get("contextTokens"), metadata.get("outputTokens")
    if (not is_hash(metadata.get("route"), "sha256:")
            or not isinstance(history, list) or not all(is_hash(value) for value in history)
            or not isinstance(tools, dict)
            or not all(isinstance(name, str) and is_hash(value, "sha256:") for name, value in tools.items())
            or not isinstance(request_only, list) or not all(is_hash(value, "sha256:") for value in request_only)
            or type(context_tokens) is not int or type(output_tokens) is not int
            or not 0 <= output_tokens <= context_tokens or context_tokens <= 0):
        return
    try:
        if metadata.get("sha256") != _context_usage_hash(message, metadata):
            return
    except (TypeError, ValueError, UnicodeError):
        return
    usage = message.usage
    usage._request_snapshot = RequestUsageSnapshot(
        route=metadata["route"], history=tuple(history), tools=dict(tools), request_only=tuple(request_only),
    )
    # Provider-specific input/cache fields are excluded from public serialization.
    # Restore their aggregate so context_tokens remains identical after replay.
    usage.input_tokens = context_tokens - output_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = usage.cache_read_input_tokens = 0
```

### history_messages

[box_agent/session_log.py:964](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L964)

```python
def history_messages(self) -> list[tuple[int, Message, dict[str, Any]]]:
    """Replay original effective history, excluding model compaction copies."""
    history = []
    compacting = False
    for event in self._active_events():
        kind, data = event["type"], event["data"]
        if kind == "surface/reset":
            history.clear()
        elif kind == "compaction/summary":
            compacting = True
        elif kind == "compaction/end":
            compacting = False
        elif (not compacting and not data.get("surfaceDerived")
              and kind in {"user/message", "assistant/message", "tool/result"}):
            payload = data if kind == "user/message" else data["message"]
            history.append((event["seq"], Message.model_validate(payload), data))
    return history
```

### SkillRead

[box_agent/skill_state.py:13](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_state.py#L13)

```python
class SkillRead:
    name: str
    source: str
    path: str
    revision: str
    prompt: str
    order: int
    reason: str
    delivered_ranges: tuple[tuple[int, int], ...] = ()
    delivered_complete: bool = False

    def log_record(self) -> dict[str, Any]:
        # These three original fields retain the old restore/hash contract.
        return {"name": self.name, "sha256": self.revision, "loadOrder": self.order,
                "source": self.source, "path": self.path, "reason": self.reason,
                "deliveredRanges": [list(pair) for pair in self.delivered_ranges],
                "deliveredComplete": self.delivered_complete}
```

### prepare_user_input

[box_agent/input_preparation.py:18](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_preparation.py#L18)

```python
def prepare_user_input(
    value: str | UserInput, workspace_dir: str | Path, permission_engine=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = UserInput.text(value) if isinstance(value, str) else value
    if not isinstance(value, UserInput):
        raise TypeError("input must be a string or UserInput")
    files = normalize_file_inputs(list(value.blocks), workspace_dir, permission_engine)
    if sum(block["type"] == "image" for block in value.blocks) + sum(
        item.get("mimeType", "").startswith("image/") for item in files
    ) > 6:
        raise ValueError("INVALID_INPUT_IMAGE_COUNT")
    images = []
    blocks = []
    for block in value.blocks:
        copied = deepcopy(block)
        if block["type"] == "image":
            try:
                data = base64.b64decode(block["data"], validate=True)
            except ValueError as exc:
                raise ValueError("INVALID_INPUT_IMAGE") from exc
            if not data or len(data) > 20 * 1024 * 1024:
                raise ValueError("INVALID_INPUT_IMAGE_SIZE")
            from .image_store import SessionImageStore
            SessionImageStore._dimensions(data, {"media_type": block["mimeType"]})
            copied = {"type": "image", "mimeType": block["mimeType"],
                      "sha256": sha256(data).hexdigest(), "imageIndex": len(images)}
            images.append({"type": "input_image", "media_type": block["mimeType"], "data": block["data"]})
        blocks.append(copied)
    context = deepcopy(value.context)
    if files:
        context["files"] = files
    image_files = [item for item in files if item.get("mimeType", "").startswith("image/")]
    positions = {item["path"]: len(images) + index for index, item in enumerate(image_files)}
    resource_images = {block["uri"]: positions[str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve())]
                       for block in blocks if block["type"] == "resource_link"
                       and str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve()) in positions}
    return {"schemaVersion": 1, "id": value.input_id or uuid4().hex,
            "fingerprint": value.fingerprint(), "blocks": blocks, "context": context,
            "workspace": str(Path(workspace_dir).resolve()), "policy": {},
            "resourceImages": resource_images}, images
```

### 所有内置字面事件写入点

下列调用保留动态表达式，不是可执行 JSON 示例。用于发现同一 type 的不同形状；user/assistant/tool 的共享入口还会合并事件级 metadata。

#### turn/start

[box_agent/agent.py:1314](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1314)

```python
self.session_log.append('turn/start', {'turn': session_turn})
```

[box_agent/tools/sub_agent_tool.py:281](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L281)

```python
child.append('turn/start', {'turn': 1})
```

#### turn/binding

[box_agent/acp/__init__.py:2661](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2661)

```python
log_store.append('turn/binding', {'turn_id': turn_id, 'prompt_fingerprint': prompt_fingerprint(params.prompt, context), 'input_fingerprint': input_value.fingerprint()})
```

#### history/revise

[box_agent/session_history.py:114](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_history.py#L114)

```python
log.append('history/revise', revision)
```

#### turn/end

[box_agent/agent.py:1539](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1539)

```python
self.session_log.append('turn/end', {'turn': session_turn, 'reason': {'kind': 'interrupted'}})
```

[box_agent/agent.py:1478](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1478)

```python
self.session_log.append('turn/end', {'turn': session_turn, 'reason': self._session_turn_reason(event.stop_reason)})
```

[box_agent/session_log.py:1350](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1350)

```python
self.append('turn/end', {'turn': open_turn, 'reason': {'kind': 'interrupted'}})
```

[box_agent/tools/sub_agent_tool.py:1070](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1070)

```python
session_log.append('turn/end', {'turn': 1, 'reason': {'kind': 'completed' if result.success else 'error'}})
```

[box_agent/tools/sub_agent_tool.py:935](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L935)

```python
session_log.append('turn/end', {'turn': 1, 'reason': {'kind': 'completed' if event.stop_reason.value == 'end_turn' else event.stop_reason.value}})
```

#### step/start

[box_agent/agent.py:1440](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1440)

```python
self.session_log.append('step/start', {'turn': session_turn, 'step': session_step})
```

[box_agent/tools/sub_agent_tool.py:1061](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1061)

```python
session_log.append('step/start', {'turn': 1, 'step': 1})
```

[box_agent/tools/sub_agent_tool.py:890](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L890)

```python
session_log.append('step/start', {'turn': 1, 'step': event.step})
```

#### step/end

[box_agent/agent.py:1535](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1535)

```python
self.session_log.append('step/end', {'turn': session_turn, 'step': session_step})
```

[box_agent/agent.py:1451](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1451)

```python
self.session_log.append('step/end', {'turn': session_turn, 'step': event.step})
```

[box_agent/agent.py:1473](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1473)

```python
self.session_log.append('step/end', {'turn': session_turn, 'step': session_step})
```

[box_agent/session_log.py:1344](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1344)

```python
self.append('step/end', {'turn': open_turn, 'step': open_step})
```

[box_agent/tools/sub_agent_tool.py:1069](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1069)

```python
session_log.append('step/end', {'turn': 1, 'step': 1})
```

[box_agent/tools/sub_agent_tool.py:902](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L902)

```python
session_log.append('step/end', {'turn': 1, 'step': event.step})
```

[box_agent/tools/sub_agent_tool.py:930](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L930)

```python
session_log.append('step/end', {'turn': 1, 'step': current_step})
```

#### user/message

[box_agent/session_log.py:996](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L996)

```python
self._append_message_event('user/message', {**payload, **metadata}, message=message, surface_op=surface_op, source_event_seqs=source_event_seqs)
```

[box_agent/session_log.py:899](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L899)

```python
self._append_message_event('user/message', payload, message=message, surface_op='append')
```

#### input/contract

[box_agent/session_log.py:727](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L727)

```python
self.append('input/contract', {'schemaVersion': 1})
```

#### input/adopted

[box_agent/context_input.py:395](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L395)

```python
self._session_store.append('input/adopted', {'id': record['id'], 'fingerprint': record['fingerprint'], 'files': record['context'].get('files', [])})
```

#### assistant/chunk

[box_agent/agent.py:1421](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1421)

```python
self.session_log.append('assistant/chunk', {'turn': session_turn, 'step': session_step, 'kind': 'thinking' if isinstance(event, ThinkingEvent) else 'text', 'content': event.content})
```

[box_agent/tools/sub_agent_tool.py:875](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L875)

```python
session_log.append('assistant/chunk', {'turn': 1, 'step': current_step, 'kind': 'thinking' if isinstance(event, ThinkingEvent) else 'text', 'content': event.content})
```

#### assistant/message

[box_agent/session_log.py:1004](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1004)

```python
self._append_message_event('assistant/message', {'turn': turn, 'step': step, 'message': payload, **metadata}, message=message, surface_op=surface_op, source_event_seqs=source_event_seqs)
```

[box_agent/session_log.py:908](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L908)

```python
self._append_message_event('assistant/message', {'turn': turn, 'step': step, 'message': payload}, message=message, surface_op='append')
```

#### tool/call

[box_agent/kernel/tool_messages.py:32](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/tool_messages.py#L32)

```python
self.session_log.append('tool/call', {'turn': self.turn, 'step': step, 'callId': call.call_id, 'name': call.name, 'arguments': call.arguments})
```

#### tool/result

[box_agent/session_log.py:1012](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1012)

```python
self._append_message_event('tool/result', {'turn': turn, 'step': step, 'message': payload, **metadata}, message=message, surface_op=surface_op, source_event_seqs=source_event_seqs)
```

[box_agent/session_log.py:1316](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1316)

```python
self.append('tool/result', {'turn': open_turn, 'step': pending['step'], 'message': Message(role='tool', content=content, tool_call_id=call_id, name=pending['name']).model_dump(mode='json', exclude_none=True), 'error': {'name': 'ToolOutcomeUnknownError' if started else 'ToolNotStartedError', 'code': code}}, surface_op='append', source_event_seqs=[pending['call_seq']] if started else None)
```

[box_agent/session_log.py:928](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L928)

```python
self._append_message_event('tool/result', data, message=message, surface_op='append')
```

#### request/header

[box_agent/kernel/loop.py:1631](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1631)

```python
session_log.append('request/header', {'turn': session_turn, 'step': step + 1, 'header': {'config': {'provider': request_provider, 'model': request_model, 'maxOutputTokens': request_max_output}, 'system': messages[0].content, 'tools': [tool.to_schema() for tool in tool_list]}})
```

[box_agent/tools/sub_agent_tool.py:1246](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1246)

```python
session_log.append('request/header', {'turn': 1, 'step': 1, 'header': {'config': {'provider': provider if isinstance(provider, str) else None, 'model': model if isinstance(model, str) else None}, 'system': messages[0].content, 'tools': []}})
```

#### request/context

[box_agent/agent.py:1023](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1023)

```python
self.session_log.append('request/context', {'imageContext': {'phase': 'continuation', 'modelRoundBaseline': model_round}})
```

[box_agent/context_input.py:122](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L122)

```python
self.image_context.session_log.append('request/context', {'imageContext': projection.manifest})
```

[box_agent/image_context.py:289](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/image_context.py#L289)

```python
self.session_log.append('request/context', {'imageContext': manifest})
```

[box_agent/kernel/loop.py:1676](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1676)

```python
session_log.append('request/context', request_context)
```

[box_agent/tools/sub_agent_tool.py:1261](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1261)

```python
session_log.append('request/context', {'turn': 1, 'step': 1, 'provider': provider if isinstance(provider, str) else None, 'model': model if isinstance(model, str) else None})
```

#### session/end-seed

[box_agent/session_log.py:1361](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1361)

```python
self.append('session/end-seed', {})
```

#### surface/reset

[box_agent/agent.py:1196](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L1196)

```python
self.session_log.append('surface/reset', {'reason': 'agent.clear_history'})
```

[box_agent/session_log.py:1068](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1068)

```python
self.append('surface/reset', {'reason': reason})
```

#### goal/change

[box_agent/agent.py:717](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L717)

```python
self.session_log.append('goal/change', {'goal': goal_payload(self.goal)})
```

[box_agent/cli.py:980](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/cli.py#L980)

```python
log.append('goal/change', {'goal': goal_payload(value)})
```

[box_agent/cli.py:2216](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/cli.py#L2216)

```python
session_log.append('goal/change', {'goal': goal_payload(agent.goal)})
```

#### plan/write

[box_agent/tools/plan_tool.py:199](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L199)

```python
self._session_log.append('plan/write', {'plan': self._store.get()})
```

#### todo/write

[box_agent/tools/todo_tool.py:538](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L538)

```python
session_log.append('todo/write', {'todos': current})
```

#### skill/change

[box_agent/agent.py:841](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L841)

```python
self.session_log.append('skill/change', {'skills': ordered})
```

[box_agent/skill_runtime.py:246](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_runtime.py#L246)

```python
self.session_log.append('skill/change', {'skills': self.log_records()})
```

[box_agent/skill_runtime.py:395](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_runtime.py#L395)

```python
self.session_log.append('skill/change', {'skills': self.log_records()})
```

#### compaction/start

[box_agent/kernel/context_engine.py:829](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L829)

```python
session_log.append('compaction/start', {'turn': session_turn, 'step': session_step, 'estimatedBefore': estimated, 'tokenLimit': token_limit})
```

[box_agent/kernel/loop.py:1123](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1123)

```python
session_log.append('compaction/start', {'turn': session_turn, 'step': step + 1, 'estimatedBefore': estimated, 'tokenLimit': history_token_limit})
```

#### compaction/summary

[box_agent/kernel/loop.py:1182](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1182)

```python
session_log.append('compaction/summary', {'turn': session_turn, 'step': step + 1, 'mode': result.mode, 'message': new_msgs[1].model_dump(mode='json', exclude_none=True), 'estimatedBefore': est_before, 'estimatedAfter': result.estimated_after, 'error': result.error})
```

#### compaction/end

[box_agent/kernel/loop.py:1202](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L1202)

```python
session_log.append('compaction/end', {'turn': session_turn, 'step': step + 1, 'mode': result.mode, 'error': result.error})
```

#### subagent/descriptor

[box_agent/tools/sub_agent_tool.py:290](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L290)

```python
child.append('subagent/descriptor', descriptor)
```

#### input/context

[box_agent/acp/__init__.py:2668](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2668)

```python
log_store.append('input/context', {'turn_id': turn_id, **context}, ignorable=True)
```

### _context_usage_hash

[box_agent/session_log.py:141](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L141)

```python
def _context_usage_hash(message: Message, metadata: dict[str, Any]) -> str:
    payload = {
        "message": message.model_dump(mode="json", exclude_none=True),
        "contextUsage": {key: value for key, value in metadata.items() if key != "sha256"},
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
```

### _append_message_event

[box_agent/session_log.py:717](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L717)

```python
def _append_message_event(
    self, event_type: str, data: dict[str, Any], *, message: Message,
    surface_op: str | dict[str, Any], source_event_seqs: list[int] | None = None,
) -> dict[str, Any]:
    pending = self._pending_message_metadata.get(id(message))
    from .input_context import requires_input_contract
    if (message.input_facts and requires_input_contract(message.input_facts)
            and not any(event["type"] == "input/contract" for event in self._events)):
        # Required feature marker: an old runtime must reject this log,
        # rather than silently dropping resource facts it cannot project.
        self.append("input/contract", {"schemaVersion": 1})
    if pending is not None:
        data.update(deepcopy(pending[1]))
    context_usage = _context_usage_metadata(message)
    if context_usage is not None:
        data["contextUsage"] = context_usage
    event = self.append(
        event_type, data, surface_op=surface_op, source_event_seqs=source_event_seqs,
    )
    self._pending_message_metadata.pop(id(message), None)
    return event
```

### _store_reference

[box_agent/skill_context.py:161](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L161)

```python
def _store_reference(self, text: str) -> dict[str, Any]:
    if self.session_store is None:
        return {}
    store = getattr(self.session_store, "store_skill_reference", None)
    if callable(store):
        return store(text)
    # SessionStorePort does not require native sidecar support. The caller
    # commits this bounded snapshot through its existing request/context.
    return {"inlineContent": text, "sha256": sha256(text.encode()).hexdigest()}
```

### inspect_image_request

[box_agent/llm/image_payload.py:93](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/image_payload.py#L93)

```python
def inspect_image_request(params: dict[str, Any], max_body_bytes: int | None) -> dict[str, Any]:
    """Measure the exact wire body without transforming any image or input."""
    size = request_body_bytes(params)
    without_images = deepcopy(params)
    images = _images(without_images)
    image_count = len(images)
    for image in images:
        image["url"] = ""
    messages = (without_images.get("extra_body") or {}).get("messages", without_images.get("messages", []))
    for message in messages:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else ():
            if (isinstance(block, dict) and block.get("type") == "image"
                    and isinstance(block.get("source"), dict) and block["source"].get("type") == "base64"):
                image_count += 1
                block["source"]["data"] = ""
    budget = max_body_bytes * 95 // 100 if max_body_bytes is not None else None
    return {
        "original_body_bytes": size, "body_bytes": size,
        "max_body_bytes": max_body_bytes, "budget_bytes": budget,
        "image_count": image_count, "non_image_bytes": request_body_bytes(without_images),
        "fits": budget is None or size <= budget, "transforms": [],
    }
```


### 输入采用恢复与锁的精确实现

#### configure_run

[box_agent/context_input.py:81](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L81)

```python
def configure_run(self, *, skill_engine: Any = None, session_store: Any = None) -> None:
    self._session_store = session_store
    self._input_adoptions = set()
    self._input_settled = set()
    for event in getattr(session_store, "events", ()):
        if event["type"] == "input/adopted":
            self._input_adoptions.add(event["data"]["id"])
        elif event["type"] == "assistant/message" and not event["data"].get("surfaceDerived"):
            self._input_settled.update(self._input_adoptions)
    if self.image_context is None:
        from .image_context import ImageContext
        self.image_context = ImageContext(session_store)
    elif self.image_context.session_log is not session_store:
        raise ValueError("Image context and final SessionStore must belong to the same session")
    self.references = (SkillReferenceContext(skill_engine, session_store=session_store)
                       if skill_engine is not None else None)
    self.prepared_tools = None
    self._history = []
```

#### _acquire_writer_lock

[box_agent/session_log.py:250](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L250)

```python
def _acquire_writer_lock(path: Path) -> BinaryIO:
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise SessionLogInUseError(
                "session already has an active writer"
            ) from exc
        raise
    return handle
```

#### _release_writer_lock

[box_agent/session_log.py:276](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L276)

```python
def _release_writer_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
```

#### close

[box_agent/session_log.py:1373](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L1373)

```python
def close(self) -> None:
    if self._closed:
        return
    self._pending_message_metadata.clear()
    try:
        self._handle.close()
    finally:
        try:
            _release_writer_lock(self._lock_handle)
        finally:
            self._closed = True
```

## 10. 本文验证范围

固定提交的实际 Message Schema 和 SessionLog 实现用于内存加载验证：26种事件示例逐一写入临时日志、flush、重新打开并 replay；完整顺序示例恢复两条消息；recover=False 的残尾截断、未知 ignorable 扩展接受及未知必需事件拒绝均通过。29个 JSON 示例及完整 JSONL 示例语法通过，本地文档链接存在。测试使用临时目录，不读写用户真实会话；不等同模型调用、工具副作用或客户端端到端验证。

针对本次勘误，另用固定源码验证：带 tool_calls 的完整消息接受对象 arguments、拒绝字符串 arguments，并可写入后重新打开及回放；持锁期间 create/open/open_with_projection 均抛 SessionLogInUseError，recover=True 不改原始字节，close 后可重新打开；直接执行固定 configure_run 函数，确认仅 adopted 或 surfaceDerived 响应不 settled，普通工具调用助手消息会 settled，已被 history/revise 撤销的物理记录仍影响集合。后一项验证的是当前限制，未修改实现。
