import { useState, useEffect, useCallback } from 'react';
import LandingPage from './pages/LandingPage';
import Workspace from './pages/Workspace';
import LoginModal from './components/LoginModal';
import * as api from './api';

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [workspaceMode, setWorkspaceMode] = useState<'local' | 'ai' | null>(null);
  const [showLogin, setShowLogin] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState('');

  // Poll login status
  useEffect(() => {
    const check = async () => {
      try {
        const s = await api.loginStatus();
        setLoggedIn(s.status === 'logged_in');
        if (s.status === 'logged_in') setWorkspaceMode(previous => previous ?? 'local');
      } catch { /* backend not ready */ }
    };
    check();
    const timer = setInterval(check, loggedIn ? 30000 : 5000);
    return () => clearInterval(timer);
  }, [loggedIn]);

  const handleLogin = useCallback(async () => {
    setLoginBusy(true);
    setLoginError('');
    try {
      const r = await api.loginStart();
      if (r.success) {
        setShowLogin(true);
      }
    } catch {
      setLoginError('收藏登录窗口未能打开，请重试。本地资料仍可使用。');
    }
    setLoginBusy(false);
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setShowLogin(false);
    setLoggedIn(true);
    setWorkspaceMode(previous => previous ?? 'local');
  }, []);

  const handleLogout = useCallback(async () => {
    if (loggedIn) {
      try { await api.logout(); } catch {}
    }
    setLoggedIn(false);
    setWorkspaceMode(null);
  }, [loggedIn]);

  return <>
        {workspaceMode === null ? <LandingPage onStartLogin={handleLogin} onTryChat={() => setWorkspaceMode('ai')}
          onOpenLocal={() => setWorkspaceMode('local')} busy={loginBusy} />
          : <Workspace onLogout={handleLogout} loggedIn={loggedIn} initialChatOnly={workspaceMode === 'ai'}
              onConnectFavorites={handleLogin} loginBusy={loginBusy} loginError={loginError} />}
        {loginError && workspaceMode === null && <p role="alert" className="p-4 text-center text-sm text-[var(--color-ink)]">{loginError}</p>}
        {showLogin && (
          <LoginModal
            onClose={() => setShowLogin(false)}
            onSuccess={handleLoginSuccess}
          />
        )}
      </>;
}
