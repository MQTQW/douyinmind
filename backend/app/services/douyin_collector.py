"""
抖音采集服务模块

核心思路：登录后不关浏览器，在同一个持久化上下文中立即抓取数据，
抓完再关。避免 Cookie/Storage 跨 session 丢失的问题。

登录 → 检测到 Cookie → 立即调 Webpack API 抓收藏夹 → 存快照 → 关浏览器
同步 → 直接返回已缓存的快照（不重新打开浏览器）
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from threading import RLock
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from app.core.config import settings
from app.services.browser_profile import persistent_profile_lock

logger = logging.getLogger(__name__)


def _find_project_chromium_executable() -> Optional[Path]:
    """在 Playwright 浏览器目录中查找 Chromium"""
    base = Path(settings.playwright_browsers_path)
    if not base.exists():
        return None
    candidates = sorted(base.glob("chromium-*/chrome-win/chrome.exe"), reverse=True)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@dataclass
class FavoriteScrapedCollection:
    platform_collection_id: str
    title: str
    video_count: int
    cover_url: Optional[str] = None


@dataclass
class FavoriteScrapedVideo:
    platform_item_id: str
    url: str
    title: str
    author: str
    duration: Optional[int] = None
    collection_ids: set[str] = field(default_factory=set)


@dataclass
class FavoriteScrapeSnapshot:
    collections: list[FavoriteScrapedCollection] = field(default_factory=list)
    videos: list[FavoriteScrapedVideo] = field(default_factory=list)
    # Only a fully validated, explicitly terminated traversal may align removals.
    # Older snapshots without proof of completion must not erase local metadata.
    complete: bool = False


class DouyinCollector:
    """抖音数据采集器 — 登录 + 抓取一体化"""

    def __init__(self) -> None:
        self._state_lock = RLock()
        self.status: str = "idle"
        self.phase: str = "idle"
        self.message: str = ""
        self._login_task: Optional[asyncio.Task] = None
        self.storage_state_path: Path = Path(settings.playwright_user_data_dir) / "state.json"
        self._snapshot: Optional[FavoriteScrapeSnapshot] = None

    def login_state(self) -> dict:
        """Read the login task and published snapshot as one state."""
        with self._state_lock:
            return {
                "status": self.status,
                "message": self.message,
                "phase": self.phase,
                "snapshot_ready": (
                    self.phase == "ready" and self.status == "logged_in"
                    and self._snapshot is not None and getattr(self._snapshot, "complete", False) is True
                ),
                "busy": self._login_task is not None and not self._login_task.done(),
            }

    def _set_login_state(
        self, status: str, phase: str, message: str,
        *, snapshot: Optional[FavoriteScrapeSnapshot] = None,
    ) -> None:
        with self._state_lock:
            self.status = status
            self.phase = phase
            self.message = message
            self._snapshot = snapshot

    # ------------------------------------------------------------------
    # 浏览器
    # ------------------------------------------------------------------

    def _browser_launch_kwargs(self) -> dict:
        """构建浏览器启动参数"""
        executable = _find_project_chromium_executable()
        if executable:
            return {"headless": False, "executable_path": str(executable)}
        channel = settings.playwright_browser_channel.strip()
        if channel:
            return {"headless": False, "channel": channel}
        if sys.platform == "win32":
            return {"headless": False, "channel": "msedge"}
        return {"headless": False}

    # ------------------------------------------------------------------
    # 登录 + 同步抓取（在同一个浏览器会话中完成）
    # ------------------------------------------------------------------

    def start_login(self) -> tuple[bool, str]:
        """启动扫码登录（后台异步），登录成功后自动抓取收藏夹快照"""
        with self._state_lock:
            if self._login_task is not None and not self._login_task.done():
                return False, "收藏连接正在进行中，请等待本次读取结束。"
            self._set_login_state("pending", "logging_in", "请在打开的浏览器窗口中扫码登录抖音")
            self._login_task = asyncio.ensure_future(self._login_flow())
            return True, self.message

    async def _login_flow(self) -> None:
        await asyncio.to_thread(self._login_and_fetch_sync)

    def _login_and_fetch_sync(self) -> None:
        with persistent_profile_lock:
            self._login_and_fetch_sync_locked()

    def _login_and_fetch_sync_locked(self) -> None:
        """
        在同一个持久化浏览器上下文中完成：登录检测 → 数据抓取

        关键：不关浏览器，登录后立即在同一个 session 中抓数据
        """
        snapshot = None
        try:
            with sync_playwright() as p:
                kwargs = self._browser_launch_kwargs()
                logger.info("启动登录浏览器: %s", kwargs)
                context = p.chromium.launch_persistent_context(
                    user_data_dir=settings.playwright_user_data_dir,
                    **kwargs,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(settings.douyin_home_url, timeout=120_000)
                    logger.info("已打开抖音首页，等待扫码登录...")

                    found = False
                    for _ in range(120):
                        has_login = any(
                            cookie.get("name") in {"sessionid", "sid_guard"}
                            for cookie in context.cookies()
                        )
                        if has_login:
                            found = True
                            break
                        time.sleep(1)
                    if not found:
                        self._set_login_state("failed", "failed", "登录等待超时，请重新连接收藏。")
                        return

                    self._set_login_state("pending", "scraping", "登录已完成，正在读取收藏夹，请稍候。")
                    try:
                        snapshot = self._fetch_in_context(page)
                        if snapshot.complete is not True:
                            raise RuntimeError("incomplete_collection_snapshot")
                    except Exception:
                        snapshot = None
                        logger.exception("抓取收藏夹失败")
                        self._set_login_state("failed", "failed", "登录已完成，收藏夹读取失败，请重新连接后重试。")

                    context.storage_state(path=str(self.storage_state_path))
                finally:
                    context.close()
            if snapshot is not None:
                self._set_login_state(
                    "logged_in", "ready",
                    f"收藏读取完成，已读取 {len(snapshot.collections)} 个收藏夹、{len(snapshot.videos)} 个视频。",
                    snapshot=snapshot,
                )
                logger.info("登录+抓取全部完成: %d 收藏夹, %d 视频", len(snapshot.collections), len(snapshot.videos))
        except Exception:
            logger.exception("登录流程异常")
            self._set_login_state("failed", "failed", "收藏连接未完成，请重新连接后重试。")

    def _fetch_in_context(self, page) -> FavoriteScrapeSnapshot:
        """
        在已有的浏览器页面中通过 Webpack API 抓取收藏夹数据

        :param page: 已登录的 Playwright Page
        :return: 收藏夹快照
        """
        # The collection route loads its API module asynchronously after DOM ready.
        page.goto("https://www.douyin.com/user/self?showTab=favorite_collection",
                  timeout=120_000, wait_until="domcontentloaded")

        # Wait for the specific module and exports before the first list request.
        result = page.evaluate("""
            async () => {
                let req = null, api = null, loadedModule = null;
                let listFn = null, videoFn = null, readinessError = "no_webpack";
                const readinessDeadline = Date.now() + 20000;
                for (let attempt = 0; attempt < 80; attempt++) {
                    const chunks = window.webpackChunkdouyin_web;
                    if (!req && Array.isArray(chunks)) {
                        const captured = chunks.push([[Symbol("douyinmind-collections")], {}, r => r]);
                        try { chunks.pop(); } catch(e) {}
                        if (typeof captured === "function" && captured.m) req = captured;
                        else readinessError = "no_require";
                    }
                    if (req) {
                        const candidates = Object.entries(req.m).filter(([, factory]) => {
                            if (typeof factory !== "function") return false;
                            const source = Function.prototype.toString.call(factory);
                            return source.includes("/aweme/v1/web/collects/list/") &&
                                source.includes("/aweme/v1/web/collects/video/list/");
                        });
                        if (candidates.length > 1)
                            return {ok:false, complete:false, error:"ambiguous_collection_module"};
                        readinessError = "module_not_found";
                        if (candidates.length === 1) {
                            const moduleId = candidates[0][0];
                            if (loadedModule !== moduleId) {
                                try { api = req(moduleId); }
                                catch(e) { return {ok:false, complete:false, error:"collection_module_unavailable"}; }
                                loadedModule = moduleId;
                            }
                            listFn = videoFn = null;
                            for (const key of Object.keys(api || {})) {
                                let value;
                                try { value = api[key]; } catch(e) { continue; }
                                if (typeof value !== "function") continue;
                                const source = Function.prototype.toString.call(value);
                                if (!listFn && source.includes("/aweme/v1/web/collects/list/")) listFn = value;
                                if (!videoFn && source.includes("/aweme/v1/web/collects/video/list/")) videoFn = value;
                            }
                            // Legacy export aliases apply only to the verified API module.
                            try {
                                listFn = listFn || api?.So;
                                videoFn = videoFn || api?.d6;
                            } catch(e) {}
                            if (typeof listFn === "function" && typeof videoFn === "function" && listFn !== videoFn
                                && Date.now() <= readinessDeadline)
                                break;
                            readinessError = "bad_exports";
                        }
                    }
                    const remaining = readinessDeadline - Date.now();
                    if (remaining <= 0 || attempt === 79) break;
                    await new Promise(resolve => setTimeout(resolve, Math.min(250, remaining)));
                }
                if (typeof listFn !== "function" || typeof videoFn !== "function" || listFn === videoFn
                    || Date.now() > readinessDeadline)
                    return {ok:false, complete:false, error:readinessError, module_wait_timeout:true};

                const incomplete = error => ({ok:false, complete:false, error});
                const more = value => value === true || value === 1 ? true :
                    value === false || value === 0 ? false : null;
                const nextCursor = (value, current, seen) => {
                    if ((typeof value !== 'number' && typeof value !== 'string') ||
                        String(value).trim() === '') return null;
                    const next = Number(value);
                    if (!Number.isSafeInteger(next) || next < 0 || next === current || seen.has(next)) return null;
                    return next;
                };

                // A partial page, unknown termination or cap is not an empty
                // remote collection. Refuse the whole snapshot before syncing.
                const collections = [];
                let cursor = 0, guard = 0;
                const collectionIds = new Set(), collectionCursors = new Set([0]);
                while (true) {
                    if (guard >= 30) return incomplete('collection_page_limit');
                    guard++;
                    const r = await listFn({cursor, offset:30});
                    if (guard >= 30) return incomplete('collection_page_limit');
                    if (!r || r.statusCode !== 0) return incomplete('list_status');
                    if (!Array.isArray(r.data) || more(r.hasMore) === null) return incomplete('list_page_invalid');
                    for (const c of r.data) {
                        const id = c && String(c.collectionFolderId ?? '').trim();
                        if (!id || collectionIds.has(id)) return incomplete('collection_identity_invalid');
                        collectionIds.add(id); collections.push(c);
                    }
                    if (collections.length >= 100) return incomplete('collection_limit');
                    if (!more(r.hasMore)) break;
                    const next = nextCursor(r.cursor, cursor, collectionCursors);
                    if (next === null || !r.data.length) return incomplete('collection_cursor_invalid');
                    cursor = next; collectionCursors.add(cursor);
                }

                // 拉每个收藏夹的视频
                const byCol = {};
                for (const c of collections) {
                    const cid = String(c.collectionFolderId);
                    const rows = [];
                    const seen = new Set();
                    const videoCursors = new Set([0]);
                    let cCur = 0, cG = 0;
                    while (true) {
                        if (cG >= 120) return incomplete('video_page_limit');
                        cG++;
                        const vr = await videoFn({collectsId:cid, cursor:cCur, offset:20});
                        if (cG >= 120) return incomplete('video_page_limit');
                        if (!vr || vr.statusCode !== 0) return incomplete('video_status');
                        if (!Array.isArray(vr.data) || more(vr.hasMore) === null) return incomplete('video_page_invalid');
                        for (const v of vr.data) {
                            const vid = String(v?.awemeId || v?.groupId || "").trim();
                            if (!/^[0-9]+$/.test(vid) || seen.has(vid)) return incomplete('video_identity_invalid');
                            seen.add(vid);
                            rows.push({
                                awemeId:vid,
                                title:String(v?.itemTitle||v?.desc||"Untitled"),
                                author:String(v?.authorInfo?.nickname||""),
                                durationMs:Number(v?.video?.duration||0)
                            });
                        }
                        if (rows.length >= 500) return incomplete('video_limit');
                        if (!more(vr.hasMore)) break;
                        const next = nextCursor(vr.cursor, cCur, videoCursors);
                        if (next === null || !vr.data.length) return incomplete('video_cursor_invalid');
                        cCur = next; videoCursors.add(cCur);
                    }
                    byCol[cid] = rows;
                }
                return {ok:true, complete:true, collections, itemsByCollection:byCol};
            }
        """)

        if not isinstance(result, dict) or result.get("ok") is not True or result.get("complete") is not True:
            error = result.get("error", "unknown") if isinstance(result, dict) else str(result)
            raise RuntimeError(f"收藏夹抓取尚未完整完成（{error}），未生成可同步快照，已有资料保留。")

        if not isinstance(result.get("collections"), list) or not isinstance(result.get("itemsByCollection"), dict):
            raise RuntimeError("收藏夹快照结构不完整，已有资料保留，请重新登录后再同步。")

        # 解析结果
        collections: list[FavoriteScrapedCollection] = []
        videos_by_id: dict[str, FavoriteScrapedVideo] = {}

        for col in (result.get("collections") or []):
            if not isinstance(col, dict):
                raise RuntimeError("收藏夹快照编号不完整，未同步已有记录。")
            cid = str(col.get("collectionFolderId") or "").strip()
            if not cid:
                raise RuntimeError("收藏夹快照编号不完整，未同步已有记录。")
            collections.append(FavoriteScrapedCollection(
                platform_collection_id=cid,
                title=str(col.get("collectionFolderName") or "收藏夹")[:255],
                video_count=max(int(col.get("videoTotal") or 0), 0),
                cover_url=col.get("cover"),
            ))
            rows = result["itemsByCollection"].get(cid)
            if not isinstance(rows, list):
                raise RuntimeError("收藏夹视频快照不完整，未同步已有记录。")
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("收藏夹视频编号不完整，未同步已有记录。")
                aid = str(row.get("awemeId") or "").strip()
                if not aid or not aid.isdigit():
                    raise RuntimeError("收藏夹视频编号不完整，未同步已有记录。")
                if aid not in videos_by_id:
                    videos_by_id[aid] = FavoriteScrapedVideo(
                        platform_item_id=aid,
                        url=f"https://www.douyin.com/video/{aid}",
                        title=str(row.get("title") or "Untitled")[:500],
                        author=str(row.get("author") or "").strip()[:255],
                        duration=self._duration_to_seconds(row.get("durationMs")),
                    )
                    videos_by_id[aid].collection_ids.add(cid)

        return FavoriteScrapeSnapshot(
            collections=collections,
            videos=list(videos_by_id.values()),
            complete=True,
        )

    @staticmethod
    def _duration_to_seconds(raw) -> Optional[int]:
        if raw is None:
            return None
        try:
            d = int(raw)
        except (TypeError, ValueError):
            return None
        if d <= 0:
            return None
        return d // 1000 if d > 1000 else d

    # ------------------------------------------------------------------
    # 同步接口（返回缓存的快照）
    # ------------------------------------------------------------------

    async def fetch_snapshot(
        self, max_collections: int = 100, max_videos_per_collection: int = 500
    ) -> FavoriteScrapeSnapshot:
        """返回登录时已抓取的快照，如果不存在则报错"""
        with self._state_lock:
            snapshot = self._snapshot
            busy = self._login_task is not None and not self._login_task.done()
            phase = self.phase
        if snapshot is None:
            if busy:
                raise RuntimeError("收藏连接正在进行中，请等待读取完成后再同步。")
            if phase == "failed":
                raise RuntimeError("本次收藏读取未完成，请重新连接后再同步。")
            raise RuntimeError("尚未登录或登录时未成功抓取，请先扫码登录")
        if getattr(snapshot, "complete", False) is not True:
            raise RuntimeError("收藏夹快照尚未完整确认，未同步已有记录；请重新登录并等待抓取完成。")
        return snapshot

    # ------------------------------------------------------------------
    # 登出
    # ------------------------------------------------------------------

    def logout(self) -> tuple[bool, str]:
        if self.login_state()["busy"]:
            return False, "收藏连接正在进行中，请等待本次读取结束后再退出。"
        if not persistent_profile_lock.acquire(blocking=False):
            return False, "抖音登录或视频下载正在使用浏览器，请完成后再退出登录。"
        try:
            return self._logout_locked()
        finally:
            persistent_profile_lock.release()

    def _logout_locked(self) -> tuple[bool, str]:
        self._login_task = None

        errors: list[str] = []
        try:
            if self.storage_state_path.exists():
                self.storage_state_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"删除登录态失败: {exc}")

        user_data_dir = Path(settings.playwright_user_data_dir)
        try:
            if user_data_dir.exists():
                shutil.rmtree(user_data_dir, ignore_errors=False)
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            errors.append(f"清理用户数据失败: {exc}")

        if errors:
            self._set_login_state("failed", "failed", "退出收藏连接未完成，请稍后重试。")
            logger.error("退出收藏连接失败，发生%d项本地清理错误", len(errors))
            return False, self.message

        self._set_login_state("idle", "idle", "已退出登录")
        return True, self.message


# 全局单例
collector = DouyinCollector()
