import { describe, expect, it } from 'vitest';
import { billedPerToken, turnTokens, usageText } from './lib';

const result = { turn_cost_usd: 0.245, usage: { input_tokens: 12, cache_read_input_tokens: 20000, cache_creation_input_tokens: 4000, output_tokens: 900 } };

describe('CLI 사용량 표시', () => {
  it('토큰은 새 입력 + 캐시 읽기 · 쓰기를 입력으로 센다', () => {
    expect(turnTokens(result.usage)).toEqual({ input: 24012, output: 900 });
    expect(turnTokens(null)).toBeNull();
  });

  it('구독 로그인인지: 세션의 apiKeySource 가 먼저, 없으면 로그인 상태, 모르면 청구 아님', () => {
    expect(billedPerToken('none', { authMethod: 'api_key' })).toBe(false);                 // 세션이 구독으로 돌았다
    expect(billedPerToken('user', { authMethod: 'claude.ai', subscriptionType: 'team' })).toBe(true);   // API 키가 끼어들었다
    expect(billedPerToken(null, { authMethod: 'claude.ai', subscriptionType: 'team', apiProvider: 'firstParty' })).toBe(false);
    expect(billedPerToken(null, { authMethod: 'api_key', apiProvider: 'firstParty' })).toBe(true);
    expect(billedPerToken(null, { authMethod: 'oauth', apiProvider: 'bedrock' })).toBe(true);
    expect(billedPerToken(null, null)).toBe(false);
  });

  it('구독이면 $ 대신 토큰 — $ 는 마우스를 올리면 API 환산 추정으로, 청구되는 로그인이면 $', () => {
    const sub = usageText(result, false, '구독 계정 (team · 예시팀)');
    expect(sub.text).toBe('토큰 25k');
    expect(sub.text).not.toContain('$');
    expect(sub.title).toContain('구독 계정 (team · 예시팀)');
    expect(sub.title).toContain('약 $0.245');
    expect(sub.title).toContain('실제 청구 아님');
    const paid = usageText(result, true);
    expect(paid.text).toBe('$0.245');
    expect(paid.title).toContain('실제로 청구');
    expect(usageText({ usage: null }, false).text).toBe('');
  });
});
