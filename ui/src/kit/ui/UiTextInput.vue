<script setup>
import { useId } from 'vue';
const controlId = `cuk-control-${useId()}`;
import InputText from 'openvue/inputtext';
defineProps({modelValue:{type:String,default:''},label:{type:String,required:true}});
const emit=defineEmits(['update:modelValue']);
</script>
<template><InputText :id="controlId" :data-semantic-anchor="'settings/control-' + controlId" :model-value="modelValue" :aria-label="label" @update:model-value="emit('update:modelValue',$event)" /></template>
