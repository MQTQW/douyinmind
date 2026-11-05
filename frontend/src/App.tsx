import { useState, useEffect, useCallback, useRef } from 'react';
import LandingPage from './pages/LandingPage';
import Workspace from './pages/Workspace';
import LoginModal from './components/LoginModal';
import * as api from './api';
import { collectionConnection, INITIAL_COLLECTION_STATUS } from './collectionConnection';

export default function App() {
  const [connectionStatus, setConnectionStatus] = useState<api.CollectionLoginStatus>(INITIAL_COLLECTION_STATUS);
  const [workspaceMode, setWorkspaceMode] = useState<'local' | 'ai' | null>(null);
  const [showLogin, setShowLogin] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState('');
  const loginRequestBusy = useRef(false);
  const statusRequest = useRef(0);
  const connection = collectionConnection(connectionStatus);
  const loggedIn = connection.ready;
  const pollInterval = connection.busy ? 1500 : loggedIn ? 30000 : 5000;
  const shownLoginError = loginError || (connection.phase === 'failed' ? connection.message : '');

  // One polling owner feeds both the modal and workspace connection state.
  useEffect(() => {
    let active = true;
    let checking = false;
    const check = async () => {
      if (checking || loginRequestBusy.current) return;
      checking = true;
      const requestId = ++statusRequest.current;
      try {
        const s = await api.loginStatus();
        if (active && requestId === statusRequest.current) {
          setConnectionStatus(s);
          if (collectionConnection(s).ready) setLoginError('');
        }
      } catch { /* backend not ready */ }
      finally { checking = false; }
    };
    void check();
    const timer = setInterval(check, pollInterval);
    return () => { active = false; clearInterval(timer); };
  }, [pollInterval]);

  useEffect(() => {
    if (!loggedIn) return;
    setShowLogin(false);
    setWorkspaceMode(previous => previous ?? 'local');
  }, [loggedIn]);

  const handleLogin = useCallback(async () => {
    if (loginRequestBusy.current) return;
    if (connection.busy) { setShowLogin(true); return; }
    loginRequestBusy.current = true;
    ++statusRequest.current;
    setLoginBusy(true);
    setLoginError('');
    setShowLogin(true);
    setConnectionStatus({ status: 'pending', phase: 'logging_in', snapshot_ready: false, busy: true,
      message: '正在打开收藏登录窗口…' });
    try {
      const r = await api.loginStart();
      setConnectionStatus(r);
      if (!r.success && !collectionConnection(r).busy) {
        const message = r.message || '收藏连接未完成，请重试读取收藏。';
        setLoginError(message);
        setConnectionStatus({ ...r, status: 'failed', phase: 'failed', snapshot_ready: false, message });
      }
    } catch {
      const message = '收藏登录窗口未能打开，请重试读取收藏。';
      setLoginError(message);
      setConnectionStatus({ status: 'failed', phase: 'failed', snapshot_ready: false, busy: false, message });
    } finally {
      loginRequestBusy.current = false;
      setLoginBusy(false);
    }
  }, [connection.busy]);

  const handleLogout = useCallback(async () => {
    if (loggedIn) {
      try {
        const result = await api.logout();
        if (!result.success) { setLoginError(result.message || '收藏退出未完成，请稍后重试。'); return; }
      } catch { setLoginError('收藏退出未完成，请稍后重试。'); return; }
      ++statusRequest.current;
      setConnectionStatus(INITIAL_COLLECTION_STATUS);
    }
    setShowLogin(false);
    setLoginError('');
    setWorkspaceMode(null);
  }, [loggedIn]);

  return <>
        {workspaceMode === null ? <LandingPage onStartLogin={handleLogin} onTryChat={() => setWorkspaceMode('ai')}
          onOpenLocal={() => setWorkspaceMode('local')} busy={loginBusy} />
          : <Workspace onLogout={handleLogout} loggedIn={loggedIn} initialChatOnly={workspaceMode === 'ai'}
              onConnectFavorites={handleLogin} loginBusy={loginBusy} loginError={shownLoginError} connection={connection} />}
        {shownLoginError && workspaceMode === null && !showLogin && <div role="alert" className="p-4 text-center text-sm text-[var(--color-ink)]"><p>{shownLoginError}</p><button className="text-button" onClick={handleLogin} disabled={loginBusy}>重试读取收藏</button></div>}
        {showLogin && (
          <LoginModal
            onClose={() => setShowLogin(false)}
            onRetry={handleLogin} connection={connection} requestBusy={loginBusy} error={loginError}
          />
        )}
      </>;
}
