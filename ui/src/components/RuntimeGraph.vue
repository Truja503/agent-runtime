<script setup lang="ts">
import { computed } from "vue";
import { VueFlow } from "@vue-flow/core";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import type { Graph } from "../types";
const props = defineProps<{ graph: Graph }>();
const emit = defineEmits<{ select: [id: string] }>();
const positions: Record<string, [number, number]> = {
  user: [20, 80],
  api: [20, 170],
  tasks: [20, 260],
  supervisor: [270, 170],
  researcher: [550, 40],
  coder: [550, 180],
  reviewer: [550, 320],
  web: [270, 330],
  broker: [850, 180],
  policy: [1100, 180],
  registry: [1350, 180],
  workspace: [1600, 180],
  approval: [850, 350],
};
const nodes = computed(() =>
  props.graph.nodes.map((n, i) => ({
    id: n.id,
    label: n.label,
    class: `node-${n.state} kind-${n.kind}`,
    position: {
      x: positions[n.id]?.[0] ?? 300 + (i % 4) * 290,
      y: positions[n.id]?.[1] ?? 510,
    },
  })),
);
const edges = computed(() =>
  props.graph.edges.map((e, i) => ({
    id: String(i),
    source: e.source,
    target: e.target,
    class: `edge-${e.state}`,
    animated: false,
    label: e.state === "idle" ? "" : e.state,
    style: {
      stroke:
        (
          {
            active: "#79b8ff",
            success: "#7ec9a2",
            failed: "#ed8b8b",
            denied: "#ed8b8b",
            waiting_approval: "#e7c477",
            interrupted: "#c4a5ed",
          } as Record<string, string>
        )[e.state] ?? "#506078",
    },
  })),
);
</script>
<template>
  <section class="graph-wrap">
    <div class="section-top">
      <span>RUNTIME TOPOLOGY</span
      ><span class="muted"
        >Drag to pan · scroll to zoom · select to inspect</span
      >
    </div>
    <VueFlow
      :nodes="nodes"
      :edges="edges"
      fit-view-on-init
      :min-zoom="0.25"
      :max-zoom="1.6"
      :nodes-connectable="false"
      @node-click="emit('select', $event.node.id)"
    />
    <div class="graph-key">
      <span>● Idle</span><span class="blue">● Active</span
      ><span class="green">● Success</span
      ><span class="red">● Denied / failed</span
      ><span class="yellow">● Waiting approval</span>
      <span class="purple">● Interrupted</span>
    </div>
  </section>
</template>
