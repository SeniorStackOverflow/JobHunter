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

  document.querySelectorAll('[data-category-picker]').forEach((picker) => {
    const rows = Array.from(picker.querySelectorAll('[data-category-row]'));
    const filter = picker.querySelector('[data-category-filter]');
    const empty = picker.querySelector('[data-category-empty]');
    const summary = picker.querySelector('[data-category-summary]');
    const counts = picker.querySelector('[data-category-counts]');
    const update = () => {
      const totals = { search: 0, auto: 0, excluded: 0 };
      rows.forEach((row) => {
        const state = row.querySelector('input:checked')?.value || 'off';
        row.dataset.state = state;
        if (state in totals) totals[state] += 1;
      });
      const text = `Ищу: ${totals.search + totals.auto} · автоотправка: ${totals.auto} · исключено: ${totals.excluded}`;
      if (summary) summary.textContent = text;
      if (counts && rows.length) counts.textContent = text;
    };
    picker.addEventListener('change', update);
    filter?.addEventListener('input', () => {
      const query = filter.value.trim().toLowerCase();
      let visible = 0;
      rows.forEach((row) => {
        const match = !query || (row.dataset.categoryName || '').includes(query);
        row.hidden = !match;
        if (match) visible += 1;
      });
      if (empty) empty.hidden = visible !== 0;
    });
    update();
  });
  const sidebar = document.querySelector('.sidebar');
  const menuButtons = document.querySelectorAll('[data-menu-toggle]');
  const setSidebar = (open) => {
    if (!sidebar) return;
    sidebar.classList.toggle('is-open', open);
    document.body.classList.toggle('sidebar-open', open);
    menuButtons.forEach((button) => button.setAttribute('aria-expanded', String(open)));
  };
  menuButtons.forEach((button) => {
    button.addEventListener('click', () => setSidebar(!sidebar?.classList.contains('is-open')));
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      setSidebar(false);
      menuButtons[0]?.focus();
    }
  });
  window.matchMedia('(min-width: 901px)').addEventListener('change', (event) => {
    if (event.matches) setSidebar(false);
  });
  document.addEventListener('click', (event) => {
    if (
      document.body.classList.contains('sidebar-open') &&
      !sidebar?.contains(event.target) &&
      !event.target.closest('[data-menu-toggle]')
    ) setSidebar(false);
  });

  const popovers = document.querySelectorAll('[data-notifications], [data-profile-picker]');
  popovers.forEach((popover) => {
    popover.addEventListener('toggle', () => {
      if (popover.open) popovers.forEach((other) => {
        if (other !== popover) other.open = false;
      });
    });
    document.addEventListener('click', (event) => {
      if (popover.open && !popover.contains(event.target)) popover.open = false;
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && popover.open) {
        popover.open = false;
        popover.querySelector('summary')?.focus();
      }
    });
  });

  document.querySelectorAll('[data-daily-limit-range]').forEach((control) => {
    const minimumInput = control.querySelector('[data-daily-minimum]');
    const maximumInput = control.querySelector('[data-daily-maximum]');
    if (!minimumInput || !maximumInput) return;

    const validateRange = () => {
      const minimum = Number.parseInt(minimumInput.value, 10);
      const maximum = Number.parseInt(maximumInput.value, 10);
      if (Number.isFinite(maximum)) minimumInput.max = String(maximum);
      const invalid =
        Number.isFinite(minimum) &&
        Number.isFinite(maximum) &&
        minimum > maximum;
      minimumInput.setCustomValidity(
        invalid ? 'Минимум откликов не может превышать максимум.' : '',
      );
    };

    minimumInput.addEventListener('input', validateRange);
    maximumInput.addEventListener('input', validateRange);
    validateRange();
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
      button.dataset.wasDisabled = String(button.disabled);
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
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form.is-submitting').forEach((form) => {
      form.classList.remove('is-submitting');
      form.removeAttribute('aria-busy');
      form.querySelectorAll('button').forEach((button) => {
        button.disabled = button.dataset.wasDisabled === 'true';
        if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
      });
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
