<template>
  <div>
    <h1>短量补位</h1>
    <p class="muted">按临期消费不够时在此登记；确认补入后新到一批，缺口量以登记时为准。未确认不占用库存。</p>

    <section v-if="pending.length" class="shelf">
      <h3>待确认</h3>
      <div v-for="x in pending" :key="x.id" class="intent-row">
        <span class="lot">{{ x.name || ('品项#' + x.item_id) }} 缺口 ×{{ x.short_qty }}{{ x.unit ? ' ' + x.unit : '' }}</span>
        <span class="muted">需求 {{ x.demand_qty }} · {{ fmt(x.created_at) }}</span>
        <input type="date" v-model="expiryOf[x.id]" :disabled="busy[x.id]" />
        <button :disabled="busy[x.id]" @click="confirm(x)">确认补入</button>
      </div>
    </section>
    <p v-else class="muted">暂无待确认意图。</p>

    <section v-if="done.length" class="shelf" style="margin-top:16px">
      <h3>已补</h3>
      <div v-for="x in done" :key="x.id" class="intent-row">
        <span class="lot">{{ x.name || ('品项#' + x.item_id) }} 补入 ×{{ x.short_qty }}{{ x.unit ? ' ' + x.unit : '' }}</span>
        <span class="badge-ok">已补 · 批 #{{ x.lot_id }}</span>
        <span class="muted">{{ fmt(x.confirmed_at) }}</span>
      </div>
    </section>
  </div>
</template>
<script setup>
import { computed, ref, onMounted } from 'vue'
import { api } from '../api'

const rows = ref([])
const busy = ref({})
const expiryOf = ref({})

const pending = computed(() => rows.value.filter(r => r.status === 'pending'))
const done = computed(() => rows.value.filter(r => r.status === 'confirmed'))

function defaultExpiry() {
  const d = new Date()
  d.setDate(d.getDate() + 30)
  return d.toISOString().slice(0, 10)
}
function fmt(ts) { return ts ? ts.replace('T', ' ').slice(0, 16) : '' }

async function load() {
  rows.value = await api('/intents')
  for (const x of rows.value) {
    if (x.status === 'pending' && expiryOf.value[x.id] === undefined) {
      expiryOf.value[x.id] = defaultExpiry()
    }
  }
}

async function confirm(x) {
  busy.value[x.id] = true
  try {
    await api(`/intents/${x.id}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ expiry: expiryOf.value[x.id] || null }),
    })
    await load()
  } catch (e) {
    alert(e.message)
  } finally {
    busy.value[x.id] = false
  }
}
onMounted(load)
</script>
