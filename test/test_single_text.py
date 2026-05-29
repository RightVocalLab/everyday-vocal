#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════
  바른발성연구소 단일 카드 톤 디버그 도구
═══════════════════════════════════════════════════════════════════

목적: 호흡 14일 코스의 특정 카드 1개만 다양한 톤으로 재캐싱해서
      ElevenLabs 크레딧 절약하며 최적 톤을 찾는다.

사용법:
  # 1일차 1번 카드 추출 + 미리보기 (캐싱 안 함)
  python test_single_text.py --course c_1 --day 1 --card 1 --preview
  
  # 1일차 1번 카드를 현재 cache_tts.py 옵션으로 캐싱
  python test_single_text.py --course c_1 --day 1 --card 1
  
  # 직접 값 지정해서 캐싱 (옛 mp3 자동 삭제 + 매니페스트 동기화 + 재캐싱)
  python test_single_text.py --course c_1 --day 1 --card 1 --stab 0.50 --style 0.10 --speed 0.95
  
  # 임의 텍스트 직접 입력 (가장 빠른 테스트)
  python test_single_text.py --text "호흡훈련 첫날입니다. 다~아~아~" --stab 0.50
  
  # 1일차 모든 카드 목록 출력
  python test_single_text.py --course c_1 --day 1 --list

환경변수: cache_tts.py와 동일 (ELEVENLABS_API_KEY, FIREBASE_KEY_PATH)
"""

import os
import sys
import re
import json
import hashlib
import argparse
import tempfile

# cache_tts.py에서 함수/상수 재사용 (해시 계산 완벽 일치 보장)
try:
    from cache_tts import (
        normalize_text, hash_text, hash_text_for_cache,
        apply_tts_replacements, call_elevenlabs,
        init_firebase, load_manifest, save_manifest,
        ELEVENLABS_VOICE_ID, ELEVENLABS_VOICE_ID_COURSE,
        ELEVENLABS_MODEL,
        ELEVENLABS_COURSE_STABILITY, ELEVENLABS_COURSE_STYLE,
        ELEVENLABS_COURSE_SPEED, ELEVENLABS_COURSE_SPEAKER_BOOST,
        MANIFEST_FILE, STORAGE_FOLDER,
    )
    from firebase_admin import storage
except ImportError as e:
    print(f"❌ cache_tts.py import 실패: {e}")
    print(f"   이 스크립트는 cache_tts.py와 같은 폴더에서 실행해야 합니다.")
    sys.exit(1)


def extract_courses_from_html(html_path):
    """v2 HTML에서 courses 배열 추출 (cache_tts.py와 동일 방식)"""
    if not os.path.exists(html_path):
        print(f"❌ HTML 파일 없음: {html_path}")
        sys.exit(1)
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()
    idx = content.find('"courses"')
    if idx < 0:
        print(f"❌ HTML에서 'courses' 키를 찾을 수 없음")
        sys.exit(1)
    start = content.find('[', idx)
    depth = 0
    end = start
    for i, ch in enumerate(content[start:], start):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(content[start:end])


def extract_cards(courses, course_id, day_num=None):
    """특정 코스의 모든 카드 추출 (day 지정 시 해당 일차만).
    
    Returns:
        list of dict: [{ 'day': int, 'stage': int, 'card_idx': int, 'text': str, 'exLabel': str }, ...]
    """
    target_course = None
    for c in courses:
        if c.get('id') == course_id:
            target_course = c
            break
    if not target_course:
        print(f"❌ 코스 '{course_id}' 없음")
        sys.exit(1)
    
    cards = []
    for item in target_course.get('items', []):
        d = item.get('dayNum', 0)
        if day_num is not None and d != day_num:
            continue
        stage = item.get('stageNum', 0)
        guide = (item.get('guide') or '').strip()
        exlabel = item.get('exLabel', '')
        if not guide:
            continue
        # [===]로 분할 (cache_tts.py와 동일)
        raw_cards = [s.strip() for s in guide.split('[===]') if s.strip()]
        for idx, raw in enumerate(raw_cards):
            # [멘트만] 마커 앞부분만 제거 (cache_tts.py와 동일)
            clean = re.sub(r'^\[멘트만\]\s*', '', raw).strip()
            if not clean:
                continue
            cards.append({
                'day': d,
                'stage': stage,
                'card_idx': idx + 1,
                'text': clean,
                'exLabel': exlabel,
            })
    return cards


def split_into_chunks(text, max_len=120):
    """v2 HTML _speakChunked + cache_tts.py와 정확히 동일한 청크 분할 로직.
    
    1. 참조 마커 제거
    2. [쉬기N] 마커를 별도 문단으로 강제 분리 (cache_tts.py 460줄 + v2 _speakChunked와 동일)
    3. 문단(\n\n) 단위 분할
    4. 문단이 max_len 초과면 문장(. ! ?) 단위 추가 분할
    """
    # 참조 마커 제거 (cache_tts.py 455줄과 동일)
    text = re.sub(r'\([^)]*참조[^)]*\)', '', text)
    # 공백 정리: \n 포함 모든 공백을 단일 공백으로 (cache_tts.py 456줄 \s+ 와 동일!)
    # ⚠️ [^\S\n] 가 아니라 \s+ 여야 앱/cache_tts.py와 청크·해시가 일치한다.
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []
    # [쉬기N] 마커를 별도 문단으로 강제 분리 (cache_tts.py 464줄과 동일, 공백정리 '이후'에 주입)
    text = re.sub(r'(\[쉬기\d+\])', r'\n\n\1\n\n', text)
    
    # 문단 분할
    paragraphs = [p.strip() for p in re.split(r'\n\n+', text) if p.strip()]
    chunks = []
    for p in paragraphs:
        if len(p) <= max_len:
            chunks.append(p)
            continue
        # 문장 단위
        sentences = [s.strip() for s in re.split(r'(?<=[.!?。])\s+|\n', p) if s.strip()]
        buf = ''
        for s in sentences:
            cand = (buf + ' ' + s).strip() if buf else s.strip()
            if len(cand) <= max_len:
                buf = cand
            else:
                if buf:
                    chunks.append(buf)
                buf = s.strip()
        if buf:
            chunks.append(buf)
    return chunks


def main():
    parser = argparse.ArgumentParser(description='단일 카드 톤 디버그')
    parser.add_argument('--html', default='vocal-runday-v2.html', help='v2 HTML 경로')
    parser.add_argument('--course', default='c_1', help='코스 ID (기본 c_1)')
    parser.add_argument('--day', type=int, help='일차 번호 (예: 1)')
    parser.add_argument('--card', type=int, help='카드 번호 (1부터, 같은 일차 안에서)')
    parser.add_argument('--chunk', type=int, help='청크 번호 (1부터). 지정하면 그 청크만 캐싱. 미지정 시 카드의 모든 청크 캐싱.')
    parser.add_argument('--text', help='카드 추출 대신 임의 텍스트 직접 입력')
    parser.add_argument('--stab', type=float, help='stability 직접 지정 (없으면 cache_tts.py 기본값)')
    parser.add_argument('--style', type=float, dest='style_val', help='style 직접 지정')
    parser.add_argument('--speed', type=float, help='speed 직접 지정')
    parser.add_argument('--voice', help='voice_id 직접 지정 (없으면 코스 본인 보이스)')
    parser.add_argument('--preview', action='store_true', help='텍스트만 출력하고 캐싱 안 함')
    parser.add_argument('--list', action='store_true', help='지정한 일차의 모든 카드 목록만 출력')
    parser.add_argument('--no-delete', action='store_true', help='옛 mp3/매니페스트 삭제 안 함 (디버그용)')
    parser.add_argument('--isolate', nargs='+', help='한자어 격리 테스트: 각 단어로 짧은 문장 생성해서 1개씩 캐싱 (예: --isolate 횡격막 상복부 복부 발성)')
    args = parser.parse_args()
    
    print("=" * 60)
    print("  단일 카드 톤 디버그")
    print("=" * 60)
    
    # --- 한자어 격리 테스트 모드 ---
    if args.isolate:
        # 파라미터 결정
        stab = args.stab if args.stab is not None else ELEVENLABS_COURSE_STABILITY
        style_v = args.style_val if args.style_val is not None else ELEVENLABS_COURSE_STYLE
        speed = args.speed if args.speed is not None else ELEVENLABS_COURSE_SPEED
        voice = args.voice if args.voice else ELEVENLABS_VOICE_ID_COURSE
        
        print(f"\n🎯 한자어 격리 테스트 모드")
        print(f"   대상 단어: {', '.join(args.isolate)}")
        print(f"   파라미터: voice={voice} stab={stab} style={style_v} speed={speed}")
        print()
        
        # Firebase 초기화
        init_firebase()
        bucket = storage.bucket()
        manifest = load_manifest()
        
        import requests
        api_key = os.environ.get('ELEVENLABS_API_KEY', '').strip()
        
        def has_jongseong(ch):
            """한글 음절의 받침 유무 판단"""
            if not ch or not ('가' <= ch <= '힣'):
                return False
            return (ord(ch) - 0xAC00) % 28 != 0
        
        for word in args.isolate:
            # 받침 유무에 따라 조사 자동 선택
            last_ch = word[-1] if word else ''
            josa = '을' if has_jongseong(last_ch) else '를'
            sentence = f"{word}{josa} 자세히 살펴봅니다."
            h = hash_text_for_cache(sentence, is_cheer=False)
            
            print(f"━" * 60)
            print(f"단어: '{word}'")
            print(f"문장: '{sentence}' ({len(sentence)}자)")
            print(f"해시: {h}")
            
            # 옛 mp3 + 매니페스트 정리
            blob = bucket.blob(f'{STORAGE_FOLDER}/{h}.mp3')
            if blob.exists():
                blob.delete()
                print(f"   🗑 기존 mp3 삭제")
            if h in manifest:
                del manifest[h]
            
            # ElevenLabs 호출
            final_text = apply_tts_replacements(sentence)
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
            payload = {
                "text": final_text,
                "model_id": ELEVENLABS_MODEL,
                "voice_settings": {
                    "stability": stab,
                    "similarity_boost": 0.75,
                    "speed": speed,
                    "style": style_v,
                    "use_speaker_boost": ELEVENLABS_COURSE_SPEAKER_BOOST,
                }
            }
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": api_key,
            }
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=60)
                if r.status_code != 200:
                    print(f"   ❌ 오류 {r.status_code}: {r.text[:200]}")
                    continue
                mp3 = r.content
                blob.upload_from_string(mp3, content_type='audio/mpeg')
                blob.cache_control = 'public, max-age=31536000'
                blob.patch()
                manifest[h] = {
                    'text': sentence,
                    'category': 'isolate_test',
                    'word': word,
                    'voice_id': voice,
                    'stability': stab,
                    'style': style_v,
                    'speed': speed,
                }
                print(f"   ✅ 캐싱 완료 ({len(mp3)/1024:.1f}KB)")
            except Exception as e:
                print(f"   ❌ 실패: {e}")
        
        save_manifest(manifest)
        print()
        print("━" * 60)
        print(f"🎉 격리 테스트 완료 ({len(args.isolate)}개 단어)")
        print()
        print("📱 앱에서 들어보는 방법:")
        print("   호흡 14일 1일차 진입 → 임시 테스트는 자유연습 페이지에서 멘트 재생")
        print("   또는 학생 숙제 시작 멘트로 들으려면 콘솔에서 직접 재생 (해시 사용)")
        print()
        print("💡 더 간단한 방법: 캐싱된 mp3 URL을 브라우저에서 직접 재생")
        print("   1) 콘솔(F12) 열기")
        print("   2) 다음 코드 실행 (각 해시별로):")
        for word in args.isolate:
            last_ch = word[-1] if word else ''
            josa = '을' if has_jongseong(last_ch) else '를'
            sentence = f"{word}{josa} 자세히 살펴봅니다."
            h = hash_text_for_cache(sentence, is_cheer=False)
            print(f"      // '{word}' ({sentence})")
            print(f"      https://firebasestorage.googleapis.com/v0/b/everyday-vocal.firebasestorage.app/o/tts-cache%2F{h}.mp3?alt=media")
        return
    
    # 1) 텍스트 결정
    if args.text:
        target_text = args.text
        label = f"커스텀 텍스트"
        print(f"\n📝 텍스트: {target_text[:100]}{'...' if len(target_text) > 100 else ''}")
    else:
        if args.day is None:
            print("❌ --day 또는 --text 중 하나는 필수")
            sys.exit(1)
        courses = extract_courses_from_html(args.html)
        cards = extract_cards(courses, args.course, args.day)
        if not cards:
            print(f"❌ {args.course} {args.day}일차에 카드 없음")
            sys.exit(1)
        
        if args.list:
            print(f"\n📋 {args.course} {args.day}일차 카드 목록 ({len(cards)}개)")
            for c in cards:
                preview = c['text'].replace('\n', ' ')[:80]
                print(f"  [{c['card_idx']}] (stage {c['stage']}, {c['exLabel']}) {preview}{'...' if len(c['text']) > 80 else ''}")
            return
        
        if args.card is None:
            print(f"❌ --card 번호 지정 필수 (또는 --list로 목록 확인)")
            print(f"\n{args.course} {args.day}일차 카드: 1 ~ {len(cards)}")
            sys.exit(1)
        
        if args.card < 1 or args.card > len(cards):
            print(f"❌ 카드 번호 범위 벗어남 (1~{len(cards)})")
            sys.exit(1)
        
        target_card = cards[args.card - 1]
        target_text = target_card['text']
        label = f"{args.course} {target_card['day']}일차 stage{target_card['stage']} card{target_card['card_idx']} ({target_card['exLabel']})"
        print(f"\n📝 대상: {label}")
        print(f"   원본 글자 수: {len(target_text)}자")
        
        # 청크 분할 (v2 HTML/cache_tts.py와 동일 로직)
        chunks = split_into_chunks(target_text, max_len=120)
        print(f"   청크 수: {len(chunks)} (참조 마커 제거 + 120자 분할 적용)")
        for i, c in enumerate(chunks, 1):
            preview = c[:70].replace(chr(10), ' ')
            print(f"     청크 {i} ({len(c)}자): {preview}{'...' if len(c) > 70 else ''}")
    
    if args.preview:
        print(f"\n--- 전체 원본 텍스트 ---")
        print(target_text)
        print(f"\n--- 청크 분할 결과 ---")
        for i, c in enumerate(chunks, 1):
            print(f"\n[청크 {i}]")
            print(c)
        print(f"\n--- 끝 ---")
        return
    
    # 카드 모드: 청크 캐싱
    if not args.text:
        # 특정 청크만 또는 전체 청크
        if args.chunk is not None:
            if args.chunk < 1 or args.chunk > len(chunks):
                print(f"\n❌ 청크 번호 범위 벗어남 (1~{len(chunks)})")
                sys.exit(1)
            target_chunks = [(args.chunk, chunks[args.chunk - 1])]
            print(f"\n🎯 청크 {args.chunk}만 캐싱")
        else:
            target_chunks = [(i+1, c) for i, c in enumerate(chunks)]
            print(f"\n🎯 {len(chunks)}개 청크 모두 캐싱")
    
    # 2) 파라미터 결정
    stab = args.stab if args.stab is not None else ELEVENLABS_COURSE_STABILITY
    style_v = args.style_val if args.style_val is not None else ELEVENLABS_COURSE_STYLE
    speed = args.speed if args.speed is not None else ELEVENLABS_COURSE_SPEED
    voice = args.voice if args.voice else ELEVENLABS_VOICE_ID_COURSE
    
    print(f"\n🎙 ElevenLabs 파라미터:")
    print(f"   voice_id  : {voice}")
    print(f"   model     : {ELEVENLABS_MODEL}")
    print(f"   stability : {stab}")
    print(f"   style     : {style_v}")
    print(f"   speed     : {speed}")
    print(f"   sp_boost  : {ELEVENLABS_COURSE_SPEAKER_BOOST}")
    
    # --text 모드: 단일 텍스트 캐싱
    # --card 모드: 청크별 캐싱 (target_chunks 리스트 사용)
    if args.text:
        cache_targets = [(1, target_text)]
    else:
        cache_targets = target_chunks
    
    # 3) Firebase 초기화 (한 번만)
    init_firebase()
    bucket = storage.bucket()
    manifest = load_manifest()
    
    import requests
    api_key = os.environ.get('ELEVENLABS_API_KEY', '').strip()
    
    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    results = []
    
    for chunk_num, chunk_text in cache_targets:
        # [쉬기N] 마커는 ElevenLabs로 보내지 않고 스킵 (앱에서 딜레이만 처리하는 마커)
        if re.match(r'^\[쉬기\d+\]$', chunk_text.strip()):
            print(f"\n청크 {chunk_num} ⏸딜레이 마커 → 스킵 (캐싱 불필요)")
            continue
        h = hash_text_for_cache(chunk_text, is_cheer=False)
        
        prefix = f"청크 {chunk_num}" if not args.text else "단일 텍스트"
        print(f"\n{prefix} ({len(chunk_text)}자)")
        print(f"  텍스트: {chunk_text[:80].replace(chr(10), ' ')}{'...' if len(chunk_text) > 80 else ''}")
        print(f"  해시  : {h}")
        
        # 옛 mp3 + 매니페스트 정리
        blob = bucket.blob(f'{STORAGE_FOLDER}/{h}.mp3')
        if not args.no_delete:
            if blob.exists():
                blob.delete()
                print(f"  🗑 기존 mp3 삭제")
            if h in manifest:
                del manifest[h]
        
        # ElevenLabs 호출
        final_text = apply_tts_replacements(chunk_text)
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
        payload = {
            "text": final_text,
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": stab,
                "similarity_boost": 0.75,
                "speed": speed,
                "style": style_v,
                "use_speaker_boost": ELEVENLABS_COURSE_SPEAKER_BOOST,
            }
        }
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=120)
            if r.status_code != 200:
                print(f"  ❌ 오류 {r.status_code}: {r.text[:200]}")
                continue
            mp3_bytes = r.content
            blob.upload_from_string(mp3_bytes, content_type='audio/mpeg')
            blob.cache_control = 'public, max-age=31536000'
            blob.patch()
            if not args.no_delete:
                manifest[h] = {
                    'text': chunk_text[:200],
                    'category': 'course_guide_debug',
                    'voice_id': voice,
                    'stability': stab,
                    'style': style_v,
                    'speed': speed,
                    'tested_at': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
                }
            print(f"  ✅ 캐싱 완료 ({len(mp3_bytes)/1024:.1f} KB)")
            results.append((chunk_num, h, chunk_text))
        except Exception as e:
            print(f"  ❌ 실패: {e}")
    
    if not args.no_delete:
        save_manifest(manifest)
    
    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"🎉 캐싱 완료: {len(results)}/{len(cache_targets)}개")
    print(f"   옵션: stab={stab} style={style_v} speed={speed}")
    print()
    print(f"📱 청크별 듣기 URL (브라우저 주소창에 붙여넣기):")
    for chunk_num, h, ctext in results:
        print(f"\n  [청크 {chunk_num}] {ctext[:50].replace(chr(10), ' ')}...")
        print(f"  https://firebasestorage.googleapis.com/v0/b/everyday-vocal.firebasestorage.app/o/tts-cache%2F{h}.mp3?alt=media")
    print()
    print(f"💡 특정 청크만 재캐싱:")
    if not args.text and args.card:
        print(f"   python test_single_text.py --course {args.course} --day {args.day} --card {args.card} --chunk N --stab 0.XX --style 0.XX")
    print(f"💡 앱에서 들어보기: 호흡 14일 → {args.day if args.day else '?'}일차 → {target_card.get('exLabel', '') if not args.text else '?'} 카드")


if __name__ == '__main__':
    main()
