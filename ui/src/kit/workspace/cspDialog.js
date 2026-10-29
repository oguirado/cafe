/** Native top-layer dialogs preserve focus and modality without inline geometry. */
import { defineComponent, h, nextTick, onMounted, ref, useId, watch } from 'vue';

/** @param {any} vendor @returns {any} */
export function createCspDialog(vendor) {
  return defineComponent({
    name: `${vendor.name || 'Dialog'}Csp`,
    inheritAttrs: false,
    props: {
      visible: Boolean, modal: Boolean,
      header: { type: String, default: '' }, footer: { type: String, default: '' },
      showHeader: { type: Boolean, default: true },
      closable: { type: Boolean, default: true },
      closeOnEscape: { type: Boolean, default: true },
      dismissableMask: Boolean, draggable: Boolean,
    },
    emits: ['update:visible', 'show', 'hide', 'after-hide'],
    setup(props, { attrs, slots, emit }) {
      const element = ref(/** @type {HTMLDialogElement|null} */ (null));
      const fallbackId = `cuk-dialog-${useId()}`;
      const close = () => emit('update:visible', false);
      const sync = async () => {
        await nextTick();
        const dialog = element.value;
        if (!dialog) return;
        if (props.visible && !dialog.open) {
          if (props.modal) dialog.showModal();
          else dialog.show();
          emit('show');
        } else if (!props.visible && dialog.open) dialog.close();
      };
      onMounted(sync);
      watch(() => props.visible, sync, { flush: 'post' });
      return () => {
        const id = String(attrs.id || fallbackId);
        const familyId = id.replace(/[^A-Za-z0-9._-]/g, '-');
        const header = props.showHeader ? h('div', { class: 'p-dialog-header' }, [
          slots.header ? slots.header() : h('span', { id: `${id}-title`, class: 'p-dialog-title' }, props.header),
          props.closable ? h('button', {
            id: `${id}-close`, 'data-semantic-anchor': `dialog/${familyId}/close`,
            type: 'button', class: 'p-dialog-close-button', 'aria-label': 'Close dialog', onClick: close,
          }, slots.closeicon ? slots.closeicon() : '×') : null,
        ]) : null;
        return h('dialog', {
          ...attrs, id, ref: element, role: 'dialog',
          class: ['p-dialog', 'cuk-csp-dialog', attrs.class],
          open: typeof document === 'undefined' && props.visible ? true : undefined,
          'aria-modal': props.modal ? 'true' : undefined,
          'aria-label': attrs['aria-label'] || (props.showHeader ? undefined : props.header || 'Dialog'),
          'aria-labelledby': attrs['aria-label'] || !props.showHeader ? undefined : `${id}-title`,
          'data-semantic-anchor': attrs['data-semantic-anchor'] || `dialog/${familyId}/panel`,
          onCancel: event => { event.preventDefault(); if (props.closeOnEscape) close(); },
          onClose: () => { if (props.visible) close(); emit('hide'); emit('after-hide'); },
          onClick: event => {
            if (!props.dismissableMask || event.target !== element.value) return;
            const box = element.value?.getBoundingClientRect();
            if (!box) return;
            if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
          },
        }, slots.container ? slots.container({ closeCallback: close }) : [
          header,
          h('div', { class: 'p-dialog-content' }, slots.default?.()),
          slots.footer || props.footer ? h('div', { class: 'p-dialog-footer' }, slots.footer?.() || props.footer) : null,
        ]);
      };
    },
  });
}
