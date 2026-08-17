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

  const sections = [...document.querySelectorAll('.admin-section')];
  const links = [...document.querySelectorAll('[data-section-link]')];
  const activateSection = () => {
    if (!sections.length) return;
    const id = (location.hash || '#overview').slice(1);
    const target = sections.find((section) => section.id === id) || sections[0];
    sections.forEach((section) => section.classList.toggle('is-active', section === target));
    links.forEach((link) => link.classList.toggle('is-active', link.getAttribute('href') === `#${target.id}`));
    const title = document.querySelector('[data-current-section]');
    const activeLink = links.find((link) => link.classList.contains('is-active'));
    if (title && activeLink) title.textContent = activeLink.dataset.title || activeLink.textContent.trim();
    setSidebar(false);
    window.scrollTo({ top: 0, behavior: 'instant' });
  };
  window.addEventListener('hashchange', activateSection);
  activateSection();

  document.querySelectorAll('[data-table-filter]').forEach((input) => {
    const table = document.getElementById(input.dataset.tableFilter);
    if (!table) return;
    const rows = [...table.querySelectorAll('tbody tr')];
    input.addEventListener('input', () => {
      const query = input.value.trim().toLocaleLowerCase();
      rows.forEach((row) => {
        row.classList.toggle(
          'hidden-by-filter',
          Boolean(query && !row.textContent.toLocaleLowerCase().includes(query)),
        );
      });
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
})();
