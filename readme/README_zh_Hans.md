## Mini Claw

**作者：** lfenghx  
**版本：** 1.1.0
**类型：** Tool（工具插件）

### 简介

Mini Claw 是一个基于Dify平台的轻量化“小龙虾”，是一个有“灵魂”的AI助手，具备短期、长期记忆，具备身份，性格，灵魂，通过他可以让你感受到AI的温度，快来领取你和你企业的专属AI助手吧~
Mini Claw具备 “Skill 渐进式披露（Progressive Disclosure）” 执行模式：把插件中的 `skills/` 目录当作“工具箱”，让Agent在需要时逐步读取技能说明、再按需读取文件/执行脚本，最终生成文本或文件交付。

### 适用场景

- 你希望快速体验爆火的小龙虾
- 你希望搭建一个专属于自己的有灵魂的AI助手，越用越顺手的好帮手
- 你希望通过skill技能包来实现更多的拓展功能

### 功能特性

- 有灵魂的AI：具备identity，soul，user等属性，可根据用户输入，调整自己的行为，提供个性化服务
- 技能调用：先用技能索引判断，再读取 SKILL.md，再按需读文件/执行命令
- 自由执行：Agent可以执行任意命令，包括但不限于读取文件、写入文件、执行脚本等
- 技能管理：具备技能的增删改查功能，可查看技能，新增技能，删除技能。具备依赖检测，和依赖自动安装功能

### 工具参数

本插件共有两个工具
“Mini_Claw”：有灵魂的AI助手，可用于沟通交流和任务执行。具备短期、长期记忆，具备身份，性格，灵魂，可根据用户输入，调整自己的行为，提供个性化服务。
“技能管理”：用于管理技能目录，可查看技能，新增技能，删除技能。具备依赖检测，和依赖自动安装功能。

![alt text](../_assets/image_0.png)

### 使用方式（在 Dify 中）

第一步：在市场中直接安装此插件
第二步：自托管用户在dify的.env中将Files_url设置为你的dify地址（需重启dify），否则dify获取不到你上传的文件
第三步：编排工作流，如下图

![alt text](../_assets/image_1.png)
![alt text](../_assets/image_1-1.png)
![alt text](../_assets/image_1-2.png)

第四步：与Mini_Claw交互，设置人格

![alt text](../_assets/image_2.png)

技巧1：可通过update_persona工具调整身份，设置灵魂（SOUL.md），让你的Mini_Claw更有温度
技巧2：可通过命令：“重置角色”，来重置Mini_Claw的身份，清除记忆，重新开始
第五步：技能管理，为Mini_Claw提供更多的自定义工具，增删查技能，可用性检测，依赖检测/安装

![alt text](../_assets/image_3.png)

特性1：自带依赖检测和自动安装功能，已取消Agent自行安装依赖的权力，请大家在使用前，自行装好依赖
🔴：不可用，需预装依赖，通过依赖安装命令自动安装，无法自动安装的，请前往plugin_daemon容器中手动安装
🟡: 不确定，Skill的YAML Front Matter不规范，无法判定
🟢：可用，依赖检测满足，如果skill.md正文中还写了其他的安装指令，则有可能出现假可用的情况，请不要把依赖写入正文
特性2：本产品兼容openclaw的skill目录结构，再次呼吁大家统一skill的标准，通过YAML Front Matter来定义技能的元数据，包括名称、描述、触发条件、需要的环境等
标准SKILL.md YAML Front Matter意如下：
---
name: agent-browser
description: 用 agent-browser CLI 做无头浏览器自动化：打开网页、交互、截图/PDF 导出
read-when :当用户提到“网页截图/打开网页/无头浏览器/自动化点击输入”
metadata: {"openclaw":{"os":["linux","darwin","win32"],"requires":{"bins":["agent-browser","node","npm"],"env":[]}}}
---
name：技能的名字
description：技能的详细描述
read-when：技能的触发条件
metadata：技能的元数据，包括操作系统、依赖等
---
此版本已不强制要求name名字等于技能文件夹的名字了，但建议保持一致
特性3：请在python技能包中放入requirements.txt依赖说明，请在node.js技能包中放入package.json依赖说明，或者node_modules目录。
特性4：dify plugin_daemon容器默认没有node环境，请自行安装，部分Cli技能包所需工具需自行在plugin_daemon中安装

### 核心用法
1.通过持久对话，让Mini_Claw越来越了解你，建立你的长期个人AI合作
2.通过技能管理，为Mini_Claw提供更多的自定义工具，请合理的进行依赖预安装，让Mini_Claw在执行时可以畅通无阻
3.Mini_Claw通过APP级实现隔离，同个APP内上下文共享

### 更新历史

- 1.0.0：Mini_Claw正式发布

### 常见问题

1.安装不上
有网络的情况下安装不上，可切换一下dify的pip源，以更好的下载依赖，内网环境下需要通过离线包安装，联系作者

2.文件传输问题
上传文件，下载文件失败，提示url不对，下载超时等，请检查dify的.env文件，是否设置了正确的Files_url，且与dify的地址一致

3.Mini_Claw没有输出
属于大模型的问题，请确保你的大模型和供应商插件支持function call功能，一般更换大模型或者调整模型供应商插件版本即可解决

4.skill调用相关
skill越完整，Agent调用越顺畅，保障你的skill相关资料，脚本没有缺失，按统一规范的标准建立skill

### 作者与联系

- GitHub：lfenghx（仓库：<https://github.com/lfenghx/mini_claw>）
- B 站：元视界\_O凌枫o
- 邮箱：550916599@qq.com
- 微信：lfeng2529230
