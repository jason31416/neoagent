"""微信通道：腾讯官方 iLink Bot API。

协议移植自 hermes-agent 的 gateway/platforms/weixin.py（MIT），
只保留 demo 需要的最小集：文本收发 + 扫码登录 + context_token。
媒体（AES 加密 CDN）、语音转写、表情回应等能力本期不搬。

协议要点（踩坑记录，改协议层前先看）：
- 收消息：长轮询 POST ilink/bot/getupdates，body 带 get_updates_buf 游标；
  游标要持久化，重启才不丢消息。
- 回消息：POST ilink/bot/sendmessage，**必须回带对方最新的 context_token**，
  否则发不出去（会话未就绪错误）。
- 登录：GET ilink/bot/get_bot_qrcode 拿二维码（扫 qrcode_img_content 那个链接），
  轮询 ilink/bot/get_qrcode_status 到 confirmed，拿 bot_token / ilink_bot_id。
"""
