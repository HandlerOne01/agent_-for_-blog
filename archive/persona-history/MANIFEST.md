# 人设历史归档 · MANIFEST（agent_-for_-blog）

- 归档时间：2026-09-26 01:17:20 +08:00
- 归档原因：发起人要求「人设文本太多了，删除之前的文本只保留爱弥斯的」——经确认含义为**文件层面的归档清理**：活动目录只留最新一版爱弥斯人设，历代草稿/备份/diff/逐字测试记录一律**移动进本目录，不删除**。
- 仓库：`agent_-for_-blog`（origin: https://gitee.com/a2186988646/agent_-for_-blog.git）
- 归档前 HEAD：`515feac docs: 记录爱弥斯人设 v4 已入库（e1588ab 实际内容为 v4，此前标题误写 v3）`
- 归档方式：全部文件原本即为 git 跟踪状态，一律用 `git mv` 移动；**保留原文件名、内容逐字节不变**（无重名冲突，未使用 `-dup2` 后缀）。
- 本目录文件数：**21**；合计 **793,973 字节**。

## 一、活动目录保留（本次未改动）

| 文件 | 字节 | sha256 | 说明 |
|---|---|---|---|
| `src/agent/common/persona.txt` | 113,097 | `6CA1D1AD9686EE9024A307E5F42CE4E1714481F46A5F23B7E41670B595AD93A9` | 线上人设 v5，正在被 `src/agent/common/persona.py` 读取 |

> `scripts/`（`build_persona_v3/v4/v5.py`、`verify_v3.py`、`check_no_verbatim*.py`、`assert_evidence.py`、`analyze_aemeath_speech.py`、`verify_db_readonly.py`）是可重跑的构建/校验资产，**未移动**，仍在 `scripts/`。
> `backups/`（博客备份及其 VERIFY.md）与人设无关，未移动。

## 二、归档清单

| 文件名（归档后） | 原路径 | 归档路径 | 字节 | sha256 |
|---|---|---|---|---|
| aemeath_speech_stats.txt | `aemeath_speech_stats.txt` | `archive/persona-history/aemeath_speech_stats.txt` | 1,632 | `6AF49B279D01B06EAD09D178C759235B2E4E92B83A76472FAE4AB0155C941ED0` |
| evidence_assertions.txt | `evidence_assertions.txt` | `archive/persona-history/evidence_assertions.txt` | 3,496 | `19BB2611409A067C4E077E7D337A3BAB18EAD7C9AA42C9ADF45142003707C103` |
| evidence_assertions_v4.txt | `evidence_assertions_v4.txt` | `archive/persona-history/evidence_assertions_v4.txt` | 3,604 | `B7ABE0B1D033606BEB5225F7362E926B432F000EE6C948D1E93E2C05CD75EE4E` |
| persona-v2-vs-v3.diff | `persona-v2-vs-v3.diff` | `archive/persona-history/persona-v2-vs-v3.diff` | 27,824 | `C5C8353CA2ACB7EE4B927712918F4AE0786812262693AB8712ABCBA4E5EC2CBB` |
| persona-v3-vs-v4.diff | `persona-v3-vs-v4.diff` | `archive/persona-history/persona-v3-vs-v4.diff` | 24,001 | `7A5BBE8321410A3F8DFEF315701F4FFD6FB4D523DFDF43B841CB982FB92FFE78` |
| persona-v4-vs-v5.diff | `persona-v4-vs-v5.diff` | `archive/persona-history/persona-v4-vs-v5.diff` | 6,420 | `1D02E7E96FC21302D8F2617169E90974829A51E1F8B4AC00347F460017249FEC` |
| persona.txt.bak-20260925 | `src/agent/common/persona.txt.bak-20260925` | `archive/persona-history/persona.txt.bak-20260925` | 1,083 | `C34E4F98E973DFB64C80547725F84850F6DB5D2F0F23D4DFA4731240E25B992C` |
| persona.txt.bak-v1-aemeath-20260925 | `src/agent/common/persona.txt.bak-v1-aemeath-20260925` | `archive/persona-history/persona.txt.bak-v1-aemeath-20260925` | 84,853 | `186BEA6F8FB334F7349A01CF9F09872E68ED2361691E36CBD78BD987FBAA921F` |
| persona.txt.bak-v2-aemeath-20260925 | `src/agent/common/persona.txt.bak-v2-aemeath-20260925` | `archive/persona-history/persona.txt.bak-v2-aemeath-20260925` | 90,736 | `CA727CB18263ED2F40EEE233450B6C4BD4A22075ACD8CA6EB0CAF34A1E868A05` |
| persona.txt.bak-v3-aemeath-20260925 | `src/agent/common/persona.txt.bak-v3-aemeath-20260925` | `archive/persona-history/persona.txt.bak-v3-aemeath-20260925` | 96,117 | `43389BD7C9B99D1C68533D9392FB02155B9634C890141F4289A27558B9567E81` |
| persona.txt.bak-v4-aemeath-20260925 | `src/agent/common/persona.txt.bak-v4-aemeath-20260925` | `archive/persona-history/persona.txt.bak-v4-aemeath-20260925` | 110,410 | `EBACC7C835935ABEF6E870608F38C32E72B6CCED06071BBC1C1771E6381F3EBE` |
| persona.txt.v3.new | `src/agent/common/persona.txt.v3.new` | `archive/persona-history/persona.txt.v3.new` | 96,117 | `43389BD7C9B99D1C68533D9392FB02155B9634C890141F4289A27558B9567E81` |
| persona.txt.v4.new | `src/agent/common/persona.txt.v4.new` | `archive/persona-history/persona.txt.v4.new` | 110,410 | `EBACC7C835935ABEF6E870608F38C32E72B6CCED06071BBC1C1771E6381F3EBE` |
| persona.txt.v5.new | `src/agent/common/persona.txt.v5.new` | `archive/persona-history/persona.txt.v5.new` | 113,097 | `6CA1D1AD9686EE9024A307E5F42CE4E1714481F46A5F23B7E41670B595AD93A9` |
| verbatim_check.txt | `verbatim_check.txt` | `archive/persona-history/verbatim_check.txt` | 445 | `9FBCD90415077EFEE36394C36DE651A7EB4F99D562333F527456DCC605DA3A27` |
| verbatim_check_v5.txt | `verbatim_check_v5.txt` | `archive/persona-history/verbatim_check_v5.txt` | 1,266 | `1100BC331FD07CF3975E9EBFD7D5325341F35259A7D0A35544DA2B9CCC145B0A` |
| verify_v3_chat.txt | `verify_v3_chat.txt` | `archive/persona-history/verify_v3_chat.txt` | 1,420 | `52222CC48567C99713F42F946DC09B66E6745344F27DE7FA89C74C62BA76E0C6` |
| verify_v3_one.txt | `verify_v3_one.txt` | `archive/persona-history/verify_v3_one.txt` | 280 | `53D33A666E0064AAE3D665405223E1784CB84D891DB698952A2191C5B60AB970` |
| verify_v3_reco.txt | `verify_v3_reco.txt` | `archive/persona-history/verify_v3_reco.txt` | 9,671 | `D46E22A5A9E5E615D47FD97F8B27CAC68B31A2C5CDE65EA8E3FEB4C0A3955386` |
| verify_v4_chat.txt | `verify_v4_chat.txt` | `archive/persona-history/verify_v4_chat.txt` | 1,420 | `52222CC48567C99713F42F946DC09B66E6745344F27DE7FA89C74C62BA76E0C6` |
| verify_v4_reco.txt | `verify_v4_reco.txt` | `archive/persona-history/verify_v4_reco.txt` | 9,671 | `D46E22A5A9E5E615D47FD97F8B27CAC68B31A2C5CDE65EA8E3FEB4C0A3955386` |

## 三、复核结果

- 归档后逐个重算 sha256：与移动前记录 **21/21 完全一致**（0 处不一致）。
- 线上人设 src/agent/common/persona.txt 未被移动，sha256 = `6CA1D1AD9686EE9024A307E5F42CE4E1714481F46A5F23B7E41670B595AD93A9`（与归档前一致）。
- 运行期核对：项目根、.venv\Scripts\python.exe -X utf8 → len(p.PERSONA) = **51762**。

## 四、如何回溯

```powershell
# 取回某一版（示例：v4 备份）
git mv archive/persona-history/persona.txt.bak-v4-aemeath-20260925 src/agent/common/persona.txt.bak-v4-aemeath-20260925
# 或直接看内容 / 与线上 diff
Get-Content archive/persona-history/persona.txt.bak-v4-aemeath-20260925 -TotalCount 20
git diff --no-index archive/persona-history/persona.txt.bak-v4-aemeath-20260925 src/agent/common/persona.txt
```

> 提醒：人设文本是进程启动时读入内存的（`persona.py` 模块级加载），**改动人设需重启进程才生效**。

