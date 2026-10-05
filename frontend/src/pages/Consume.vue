<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :key="i.id" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <pre v-if="result">{{ result }}</pre>
    <div v-if="shortfall" class="shortfall-card">
      <strong>库存不足，缺口 {{ shortfall.short }} {{ unitOf(item_id) }}</strong>
      <p class="muted">已登记「{{ nameOf(item_id) }}」补位意图，确认补入后将新到一批。失败当次未扣减在架库存。</p>
      <button @click="toIntents">去补位</button>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const result = ref('')
const shortfall = ref(null)
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })
function nameOf(id) { return items.value.find(i => i.id === id)?.name || id }
function unitOf(id) { return items.value.find(i => i.id === id)?.unit || '' }
function toIntents() { router.push('/intents') }
async function go() {
  result.value = ''; shortfall.value = null
  try {
    result.value = JSON.stringify(await api('/consume', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }) }), null, 2)
  } catch (e) {
    if (e.status === 409 && e.data?.reason === 'short') {
      shortfall.value = e.data
    } else {
      result.value = e.message
    }
  }
}
</script>
