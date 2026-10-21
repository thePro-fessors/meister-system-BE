/**
 * scripts/load_test_k6.js - 전교생 500명 동시 접속 제출 마감일 부하 테스트 스크립트 (k6)
 * 
 * 시나리오:
 * 1. 500명의 가상 학생(VUs)이 3분 동안 단계적으로 동시 유입 (Ramping VUs)
 * 2. 학생 로그인 (POST /api/auth/login)
 * 3. 마이스터 취득 현황 대시보드 조회 (GET /api/students/me)
 * 4. 내 제출 내역 목록 조회 (GET /api/submissions)
 * 5. 마감 직전 증빙자료 동시 제출 (POST /api/submissions)
 * 
 * 실행 방법:
 * k6 run scripts/load_test_k6.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// 커스텀 성능 메트릭 정의
const submissionDuration = new Trend('submission_duration_ms');
const errorRate = new Rate('error_rate');
const successfulSubmissions = new Counter('successful_submissions');

export const options = {
  stages: [
    { duration: '30s', target: 100 },  // 30초 동안 100명까지 램프업
    { duration: '1m', target: 500 },   // 1분 동안 전교생 500명 피크 부하
    { duration: '1m', target: 500 },   // 500명 상태 1분간 스트레스 유지
    { duration: '30s', target: 0 },    // 30초 동안 정상 램프다운
  ],
  thresholds: {
    // 95%의 요청이 800ms 이내에 완료되어야 함
    http_req_duration: ['p(95)<800'],
    // 전체 에러율은 1% 미만이어야 함
    error_rate: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost:8000';

export default function () {
  const vuId = __VU; // 1 ~ 500
  const grade = ((vuId % 3) + 1); // 1, 2, 3학년 분산
  const classNo = ((vuId % 4) + 1); // 1~4반 분산
  const studentNo = ((vuId % 20) + 1);
  const email = `s${grade}${String(classNo).padStart(2, '0')}${String(studentNo).padStart(2, '0')}@bssm.hs.kr`;

  // 1. 학생 로그인
  const loginPayload = JSON.stringify({
    username: email,
    password: `bssm2026!`,
  });

  const loginRes = http.post(`${BASE_URL}/api/auth/login`, loginPayload, {
    headers: { 'Content-Type': 'application/json' },
  });

  const loginOk = check(loginRes, {
    'login status is 200': (r) => r.status === 200,
  });

  if (!loginOk) {
    errorRate.add(1);
    sleep(1);
    return;
  }

  const token = loginRes.json('data.accessToken') || loginRes.json('data.token');
  const authHeaders = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
  };

  // 2. 대시보드 조회
  const meRes = http.get(`${BASE_URL}/api/students/me`, { headers: authHeaders });
  check(meRes, {
    'dashboard status is 200': (r) => r.status === 200,
  });

  // 3. 제출 내역 조회
  const subListRes = http.get(`${BASE_URL}/api/submissions`, { headers: authHeaders });
  check(subListRes, {
    'submissions list is 200': (r) => r.status === 200,
  });

  sleep(0.5); // 사용자 폼 입력 지연 모사

  // 4. 증빙자료 신규 제출 스트레스 테스트
  const submitPayload = {
    itemId: '1',
    detail: `k6 부하 테스트 가상 증빙 제출 (VU ${vuId})`,
    activityDate: '2026-03-20',
    description: `동시 접속 500명 제출 마감일 스트레스 테스트 데이터입니다. VU=${vuId}`,
  };

  const startSubmit = Date.now();
  const submitRes = http.post(`${BASE_URL}/api/submissions`, submitPayload, {
    headers: { 'Authorization': `Bearer ${token}` },
  });
  const duration = Date.now() - startSubmit;
  submissionDuration.add(duration);

  const submitSuccess = check(submitRes, {
    'submission created (200 or 201)': (r) => r.status === 200 || r.status === 201,
  });

  if (submitSuccess) {
    successfulSubmissions.add(1);
    errorRate.add(0);
  } else {
    errorRate.add(1);
  }

  sleep(1);
}
