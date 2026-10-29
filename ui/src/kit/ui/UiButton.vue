<script setup>
import { useId } from 'vue';
const controlId = `cuk-control-${useId()}`;
import Button from 'openvue/button';
const props=defineProps({label:{type:String,required:true},intent:{type:String,default:'neutral'},loading:Boolean});
</script>
<template><Button :id="controlId" :data-semantic-anchor="'settings/control-' + controlId" :label="label" :loading="loading" :severity="intent === 'danger' ? 'danger' : intent === 'primary' ? undefined : 'secondary'" size="small"><slot>{{label}}</slot></Button></template>
