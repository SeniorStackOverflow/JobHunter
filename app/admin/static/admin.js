(() => {
  const root = document.documentElement;
  let savedTheme = null;
  try {
    savedTheme = localStorage.getItem('jh-theme');
  } catch {
    // Storage may be unavailable in hardened/private browser contexts.
  }
  if (savedTheme === 'light' || savedTheme === 'dark') root.dataset.theme = savedTheme;

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try {
        localStorage.setItem('jh-theme', next);
      } catch {
        // The visual toggle must continue to work even when storage is unavailable.
      }
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

  document.querySelectorAll('[data-profile-select]').forEach((select) => {
    select.addEventListener('change', () => select.form?.requestSubmit());
  });

  document.querySelectorAll('[data-daily-limit-range]').forEach((control) => {
    const minimumInput = control.querySelector('[data-daily-minimum]');
    const maximumInput = control.querySelector('[data-daily-maximum]');
    const forceInput = control.querySelector('[data-daily-force]');
    if (!minimumInput || !maximumInput || !forceInput) return;

    const validateRange = () => {
      const minimum = Number.parseInt(minimumInput.value, 10);
      const maximum = Number.parseInt(maximumInput.value, 10);
      if (Number.isFinite(maximum)) minimumInput.max = String(maximum);
      const invalid =
        forceInput.checked &&
        Number.isFinite(minimum) &&
        Number.isFinite(maximum) &&
        minimum > maximum;
      minimumInput.setCustomValidity(
        invalid ? 'Минимум откликов не может превышать максимум.' : '',
      );
    };

    const syncMinimumAvailability = () => {
      const enabled = forceInput.checked;
      minimumInput.readOnly = !enabled;
      minimumInput.setAttribute('aria-disabled', String(!enabled));
      minimumInput.closest('label')?.classList.toggle('is-inactive', !enabled);
      validateRange();
    };

    minimumInput.addEventListener('input', validateRange);
    maximumInput.addEventListener('input', validateRange);
    forceInput.addEventListener('change', syncMinimumAvailability);
    syncMinimumAvailability();
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
  const reviewReasonField = dialog?.querySelector('[data-review-reason-field]');
  const reviewReasonInputs = dialog?.querySelectorAll('input[name="dialog-review-reason"]') || [];
  const reviewLearnInput = dialog?.querySelector('[data-review-learn]');
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
      const asksForReviewReason = Object.prototype.hasOwnProperty.call(
        form.dataset,
        'reviewReject',
      );
      if (reviewReasonField) reviewReasonField.hidden = !asksForReviewReason;
      if (asksForReviewReason) {
        reviewReasonInputs.forEach((input) => {
          input.checked = input.value === 'other';
        });
        if (reviewLearnInput) reviewLearnInput.checked = true;
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
    if (reviewReasonField && !reviewReasonField.hidden) {
      const selectedReason = Array.from(reviewReasonInputs).find((input) => input.checked);
      const reasonTarget = pending.form.querySelector('input[name="reason_code"]');
      const learnTarget = pending.form.querySelector('input[name="learn_from_review"]');
      if (reasonTarget) reasonTarget.value = selectedReason?.value || 'other';
      if (learnTarget) learnTarget.value = String(reviewLearnInput?.checked !== false);
    }
    confirmedForm = pending.form;
    pending.form.requestSubmit(pending.submitter || undefined);
  });
})();
