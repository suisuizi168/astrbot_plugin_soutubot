"""Source-neutral download-to-PDF delivery orchestration."""
from __future__ import annotations
import asyncio
import uuid
from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from .file_delivery import send_pdf, safe_sender_id
from .download_service import AlbumNotFoundError, PartialDownloadError, DownloadTimeoutError, PdfGenerationError
from .workflow_common import JmDownloadReplyFilter, confirmation_duration

class DownloadWorkflow:
    def _download_confirmation_notice(self, source, album_id):
        duration = confirmation_duration(getattr(self, "_download_confirmation_timeout", 600))
        return (
            f"📥 是否需要下载 {source.upper()}{album_id}？\n"
            f"请由发起查询的人在当前聊天中，{duration}内回复“下载”。\n"
            "不回复就不会下载，超时后请重新查询。"
        )

    async def _send_download_confirmation(self, umo, sender_id, source, album_id, message=None):
        """Offer the selected route, never start work before a scoped reply."""
        if not sender_id:
            # Without an authenticated requester, offer an explicit command only.
            await self._send_chain(umo, MessageChain([Comp.Plain(f"需要下载可发送：/{source} {album_id}")]))
            return
        key = (umo, sender_id)
        timeout = getattr(self, "_download_confirmation_timeout", 600)
        entry = JmDownloadReplyFilter.remember(umo, sender_id, album_id, source=source, timeout_seconds=timeout)
        try:
            await self._send_chain(umo, MessageChain([Comp.Plain(message or self._download_confirmation_notice(source, album_id))]))
        except BaseException:
            if JmDownloadReplyFilter.pending.get(key) is entry:
                JmDownloadReplyFilter.pending.pop(key, None)
            raise
        if JmDownloadReplyFilter.pending.get(key) is entry:
            # Give the requester the full configured window after delivery.
            JmDownloadReplyFilter.remember(umo, sender_id, album_id, source=source, timeout_seconds=timeout)
        logger.info("[下载确认] 提示已发送：%s%s，有效期=%s秒", source.upper(), album_id, timeout)

    async def _start_jm_download(self, event, album_id):
        return await self._queue_download(event.unified_msg_origin, safe_sender_id(event), "jm", album_id)

    async def _queue_download(self, umo, sender_id, source, album_id):
        label = source.upper()
        if not getattr(self, f"_{source}_download_enabled", False):
            return f"{label} 下载功能当前已关闭。"
        if self._closing:
            return "插件正在关闭，暂时不能接收下载任务。"
        if not hasattr(self, "_deliveries"):
            self._deliveries, self._delivery_states = {}, {}
        key = (umo, source, album_id)
        if key in self._deliveries:
            return f"{label}{album_id} 已在当前会话处理中，完成后会自动发送 PDF。"
        if len(self._deliveries) >= getattr(self, "_max_pending_tasks", 20):
            return "待发送任务较多，请稍后再试。"
        # Reserve before the first await, so repeated calls cannot queue two sends.
        self._deliveries[key] = None
        try:
            service = getattr(self, f"_{source}_download_service")
            acquired = await service.acquire(album_id, trace_id=uuid.uuid4().hex[:8])
            if callable(getattr(service, "hold", None)):
                service.hold(album_id)
            state_key = (source, album_id)
            state = self._delivery_states.setdefault(state_key, {"count": 0, "failed": False})
            state["count"] += 1
            task = asyncio.create_task(self._background_download(
                umo=umo, sender_id=sender_id, source=source, album_id=album_id,
                acquisition=acquired, delivery_key=key, state_key=state_key))
            self._deliveries[key] = task
            self._track_task(task)
            if acquired.status == "cache": return None
            icon = "⏳" if acquired.status == "joined" else "⬇️"
            return f"{icon} {label}{album_id} {'已在处理中' if acquired.status == 'joined' else '正在下载处理中'}，完成后会自动发送 PDF。"
        except asyncio.CancelledError:
            self._deliveries.pop(key,None)
            raise
        except Exception as exc:
            self._deliveries.pop(key,None)
            logger.warning("%s 下载任务启动失败: %s", label, type(exc).__name__)
            return f"{label}{album_id} 下载暂时不可用。"

    async def _background_jm_download(self, *, umo, album_id, trace_id=None, acquisition=None, sender_id=""):
        await self._background_download(umo=umo, album_id=album_id, source="jm", sender_id=sender_id, acquisition=acquisition)

    async def _background_download(self, *, umo, album_id, source, sender_id="", acquisition=None, delivery_key=None, state_key=None):
        label=source.upper()
        artifact=None
        sent=False
        try:
            service=getattr(self,f"_{source}_download_service")
            artifact=await acquisition.wait() if acquisition is not None else await service.get_or_download(album_id)
            limit=getattr(self, f"_{source}_max_pdf_size", getattr(self,"_jm_max_pdf_size",200*1024*1024))
            if artifact.pdf_size>limit:
                await self._send_chain(umo,MessageChain([Comp.Plain(f"{label}{album_id} 已生成 PDF，但超过 {limit/1024/1024:.0f} MB 的发送上限，文件已保留。")]))
                return
            password=getattr(artifact,"password","") or "无"
            notice=f"📚 {artifact.title or label+album_id}\n📄 {artifact.pdf_size/1024/1024:.1f} MB\n✅ 已成功生成 PDF，开始上传...\n🔑 密码：{password}"
            sent,_=await send_pdf(send=self._send_chain,umo=umo,pdf=artifact.pdf_path,jm_id=album_id,
                source=label,sender_id=sender_id,notice=notice,cache=artifact.cache_hit,
                private_fallback=bool(sender_id) and self._policy.is_allowed(sender_id=sender_id))
            if not sent:
                await self._send_chain(umo,MessageChain([Comp.Plain(f"⚠️ {label}{album_id} 未能确认 PDF 发送成功，请先检查群文件或私聊消息，避免重复下载。")]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc,AlbumNotFoundError): message=f"❌ 未找到 {label}{album_id}，请确认编号是否正确。"
            elif isinstance(exc,PartialDownloadError): message=f"❌ {label}{album_id} 下载不完整，本次未能生成 PDF。\n可以稍后重新发送 /{source} {album_id} 重试。"
            elif isinstance(exc,DownloadTimeoutError): message=f"❌ {label}{album_id} 处理超时，请稍后重试。"
            elif isinstance(exc,PdfGenerationError): message=f"❌ {label}{album_id} PDF 生成失败，请稍后重试。"
            else: message=f"❌ {label}{album_id} 暂时无法处理，请稍后重试。"
            logger.warning("%s 下载/生成/发送失败: %s",label,type(exc).__name__)
            try: await self._send_chain(umo,MessageChain([Comp.Plain(message)]))
            except Exception: logger.warning("下载失败提示未能发送")
        finally:
            cleanup=sent
            if state_key is not None:
                state=self._delivery_states[state_key]
                state['count']-=1
                state['failed'] |= not sent
                cleanup=sent and state['count']==0 and not state['failed']
                if state['count']==0: self._delivery_states.pop(state_key,None)
            if artifact and cleanup and getattr(self,f"_{source}_cleanup_pdf_after_send",False):
                try: artifact.pdf_path.unlink(missing_ok=True)
                except OSError: logger.warning("文件已发送，PDF 清理失败")
            if delivery_key is not None: self._deliveries.pop(delivery_key,None)
            if delivery_key is not None and callable(getattr(service, "release", None)):
                service.release(album_id)

    async def _send_jm_download_error(self, umo, trace_id, album_id, message, *, error=False):
        await self._send_chain(umo,MessageChain([Comp.Plain(message)]))
