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

  document.querySelectorAll('[data-notice-dismiss]').forEach((button) => {
    button.addEventListener('click', () => {
      button.closest('[data-action-notice]')?.remove();
      const url = new URL(window.location.href);
      url.searchParams.delete('notice');
      url.searchParams.delete('google');
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
    });
  });

  const dialog = document.querySelector('[data-confirm-dialog]');
  const dialogTitle = dialog?.querySelector('[data-confirm-title]');
  const dialogMessage = dialog?.querySelector('[data-confirm-message]');
  const dialogIcon = dialog?.querySelector('[data-confirm-icon]');
  const dialogAccept = dialog?.querySelector('[data-confirm-accept]');
  const dialogCancel = dialog?.querySelector('[data-confirm-cancel]');
  const reasonField = dialog?.querySelector('[data-confirm-reason-field]');
  const reasonInput = dialog?.querySelector('[data-confirm-reason-input]');
  let pendingConfirmation = null;
  let confirmedForm = null;

  const setSubmitting = (form, submitter) => {
    form.classList.add('is-submitting');
    form.setAttribute('aria-busy', 'true');
    form.querySelectorAll('button').forEach((button) => {
      button.disabled = true;
    });
    if (submitter) {
      submitter.dataset.originalLabel = submitter.textContent;
      submitter.textContent = submitter.dataset.pendingLabel || 'Выполняется…';
    }
  };

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const submitter = event.submitter || form.querySelector('button[type="submit"], button:not([type])');
      if (confirmedForm === form) {
        confirmedForm = null;
        setSubmitting(form, submitter);
        return;
      }
      if (!form.dataset.confirm) {
        setSubmitting(form, submitter);
        return;
      }

      event.preventDefault();
      if (!dialog || typeof dialog.showModal !== 'function') return;
      pendingConfirmation = { form, submitter };
      dialog.dataset.tone = form.dataset.confirmTone || 'default';
      if (dialogTitle) dialogTitle.textContent = form.dataset.confirmTitle || 'Подтвердите действие';
      if (dialogMessage) dialogMessage.textContent = form.dataset.confirm;
      if (dialogIcon) dialogIcon.textContent = form.dataset.confirmTone === 'danger' ? '!' : '?';
      if (dialogAccept) {
        dialogAccept.textContent = form.dataset.confirmAction || submitter?.textContent || 'Подтвердить';
        dialogAccept.className = `btn ${form.dataset.confirmTone === 'danger' ? 'btn-danger' : 'btn-primary'}`;
      }
      if (reasonField && reasonInput) {
        const asksForReason = Object.prototype.hasOwnProperty.call(
          form.dataset,
          'confirmReason',
        );
        reasonField.hidden = !asksForReason;
        reasonInput.value = '';
        reasonInput.placeholder = form.dataset.confirmReason || 'Коротко укажите причину';
      }
      dialog.returnValue = '';
      dialog.showModal();
      (reasonField && !reasonField.hidden ? reasonInput : dialogCancel)?.focus();
    });
  });

  dialogCancel?.addEventListener('click', () => dialog?.close('cancel'));
  dialogAccept?.addEventListener('click', () => dialog?.close('confirm'));
  dialog?.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close('cancel');
  });
  dialog?.addEventListener('close', () => {
    const pending = pendingConfirmation;
    pendingConfirmation = null;
    if (!pending || dialog.returnValue !== 'confirm') return;
    if (reasonField && !reasonField.hidden && reasonInput) {
      let target = pending.form.querySelector('input[name="reason"]');
      if (!target) {
        target = document.createElement('input');
        target.type = 'hidden';
        target.name = 'reason';
        pending.form.append(target);
      }
      target.value = reasonInput.value.trim();
    }
    confirmedForm = pending.form;
    pending.form.requestSubmit(pending.submitter || undefined);
  });
})();
