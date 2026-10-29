(() => {
  'use strict';
  const token = document.currentScript?.dataset.csrf;
  if (!token) return;
  let stopped = false, pending = false;
  async function heartbeat() {
    if (document.hidden || stopped || pending) return;
    pending = true;
    try {
      const response = await fetch('/api/account/heartbeat', {method:'POST', headers:{'X-CSRF-Token':token}});
      if (response.status === 401 || response.status === 403) stopped = true;
    } catch { /* The next visible-page heartbeat retries a transient connection failure. */ }
    finally { pending = false; }
  }
  setInterval(heartbeat, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) heartbeat(); });
})();
