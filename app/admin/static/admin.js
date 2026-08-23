(() => {
  const root = document.documentElement;
  const savedTheme = localStorage.getItem('jh-theme');
  if (savedTheme === 'light' || savedTheme === 'dark') root.dataset.theme = savedTheme;

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      localStorage.setItem('jh-theme', next);
    });
  });

  const sidebar = document.querySelector('.sidebar');
  const setSidebar = (open) => {
    if (!sidebar) return;
    sidebar.classList.toggle('is-open', open);
    document.body.classList.toggle('sidebar-open', open);
  };
  document.querySelectorAll('[data-menu-toggle]').forEach((button) => {
    button.addEventListener('click', () => setSidebar(!sidebar?.classList.contains('is-open')));
  });
  document.addEventListener('click', (event) => {
    if (
      document.body.classList.contains('sidebar-open') &&
      !sidebar?.contains(event.target) &&
      !event.target.closest('[data-menu-toggle]')
    ) setSidebar(false);
  });

  document.querySelectorAll('[data-password-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = button.closest('.password-input')?.querySelector('input');
      if (!input) return;
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.textContent = show ? 'Скрыть' : 'Показать';
      button.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
})();
