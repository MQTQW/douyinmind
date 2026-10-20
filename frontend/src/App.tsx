import { useState, useEffect, useCallback } from 'react';
import LandingPage from './pages/LandingPage';
import Workspace from './pages/Workspace';
import LoginModal from './components/LoginModal';
import * as api from './api';

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [chatOnly, setChatOnly] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);

  // Poll login status
  useEffect(() => {
    const check = async () => {
      try {
        const s = await api.loginStatus();
        setLoggedIn(s.status === 'logged_in');
      } catch { /* backend not ready */ }
    };
    check();
    const timer = setInterval(check, loggedIn ? 30000 : 5000);
    return () => clearInterval(timer);
  }, [loggedIn]);

  const handleLogin = useCallback(async () => {
    setLoginBusy(true);
    try {
      const r = await api.loginStart();
      if (r.success) {
        setShowLogin(true);
      }
    } catch (e) {
      console.error(e);
    }
    setLoginBusy(false);
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setShowLogin(false);
    setLoggedIn(true);
  }, []);

  const handleLogout = useCallback(async () => {
    if (loggedIn) {
      try { await api.logout(); } catch {}
    }
    setLoggedIn(false);
    setChatOnly(false);
  }, [loggedIn]);

  if (!loggedIn && !chatOnly) {
    return (
      <>
        <LandingPage onStartLogin={handleLogin} onTryChat={() => setChatOnly(true)} busy={loginBusy} />
        {showLogin && (
          <LoginModal
            onClose={() => setShowLogin(false)}
            onSuccess={handleLoginSuccess}
          />
        )}
      </>
    );
  }

  return <Workspace onLogout={handleLogout} loggedIn={loggedIn} />;
}
