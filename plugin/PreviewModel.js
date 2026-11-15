.pragma library

function boundedRows(values, maximum) {
  // A var signal exposes JSON arrays as Qt sequences, not JavaScript Arrays.
  if (!values || typeof values !== "object" || typeof values.length !== "number") return []
  return Array.prototype.slice.call(values, 0, maximum)
}
