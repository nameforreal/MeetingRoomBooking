import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://proxy:8080";

export const options = {
  discardResponseBodies: true,
  stages: [
    { duration: "20s", target: 100 },
    { duration: "20s", target: 300 },
    { duration: "20s", target: 500 },
    { duration: "30s", target: 1000 },
    { duration: "60s", target: 1000 },
    { duration: "10s", target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
};

export function setup() {
  const response = http.get(`${BASE_URL}/health/`, { tags: { endpoint: "health" } });
  if (response.status !== 200) {
    throw new Error(`Isolated application health check failed: HTTP ${response.status}`);
  }
}

export default function () {
  const path = Math.random() < 0.8 ? "/api/rooms/" : "/rooms/";
  const response = http.get(`${BASE_URL}${path}`, { tags: { endpoint: path } });
  check(response, {
    "room list returns HTTP 2xx": (result) => result.status >= 200 && result.status < 300,
  });
  // About one read request per virtual user every 10 seconds: ~100 RPS at 1000 VUs.
  sleep(8 + Math.random() * 4);
}
