"""Small, nonce-protected progressive enhancement for slow form submissions."""
SCRIPT = r"""
(() => {
  const timers = new Map();
  const statuses = new Map();
  function reset(form) {
    clearInterval(timers.get(form)); timers.delete(form);
    form.removeAttribute('aria-busy'); delete form.dataset.pending;
    form.querySelectorAll('button[data-busy-button]').forEach(button => {
      button.disabled = false; button.textContent = button.dataset.originalText;
      delete button.dataset.busyButton;
    });
    statuses.get(form)?.remove(); statuses.delete(form);
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    if (form.dataset.pending) { event.preventDefault(); return; }
    form.dataset.pending = '1'; form.setAttribute('aria-busy', 'true');
    const text = form.dataset.loadingText || '正在保存，请稍候…';
    form.querySelectorAll('button[type="submit"]').forEach(button => {
      button.dataset.originalText = button.textContent; button.dataset.busyButton = '1';
      button.disabled = true;
      const spinner = document.createElement('span'); spinner.className = 'spinner';
      spinner.setAttribute('aria-hidden', 'true');
      button.replaceChildren(spinner, document.createTextNode(text));
    });
    const status = document.createElement('p'); status.className = 'loading-status';
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    status.textContent = '正在处理，请不要重复点击。'; form.after(status); statuses.set(form, status);
    const start = Date.now();
    timers.set(form, setInterval(() => {
      const seconds = Math.floor((Date.now() - start) / 1000);
      status.textContent = seconds < 30 ? `已等待 ${seconds} 秒，完成后自动显示结果。` :
        `已等待 ${seconds} 秒，仍在等待处理结果。请不要重复提交；离开页面不会撤回已发出的请求。`;
    }, 1000));
    // Inputs remain enabled so native form submission still includes all fields.
    document.querySelectorAll('form').forEach(other => {
      if (other === form) return;
      other.querySelectorAll('button[type="submit"]').forEach(button => {
        button.dataset.originalText = button.textContent; button.dataset.busyButton = '1'; button.disabled = true;
      });
    });
  });
  window.addEventListener('pageshow', () => document.querySelectorAll('form').forEach(reset));
})();
"""
