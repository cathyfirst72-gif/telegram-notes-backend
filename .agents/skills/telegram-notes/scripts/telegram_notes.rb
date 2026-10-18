#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'net/http'
require 'uri'
require 'time'
require 'fileutils'
require 'open3'
require 'tempfile'

ROOT = File.expand_path(ENV.fetch('TELEGRAM_NOTES_PROJECT', Dir.pwd))
NOTES = File.join(ROOT, 'telegram-notes')
STATE = File.join(NOTES, '.state.json')
LAST_UPDATE_ID = File.join(NOTES, '.last_update_id')
PENDING = File.join(NOTES, '.pending.json')
CLASSIFIED = File.join(NOTES, '.classified.json')
SKILL_DIR = File.expand_path('..', __dir__)
PYTHON = File.join(SKILL_DIR, '.venv', 'bin', 'python')
TRANSCRIBE = File.join(__dir__, 'transcribe.py')
MODEL_DIR = File.join(SKILL_DIR, '.models', 'base')
class NotesError < StandardError; end

def read_json(path)
  JSON.parse(File.read(path))
end

def private_write(path, content)
  File.open(path, File::WRONLY | File::CREAT | File::TRUNC, 0o600) { |file| file.write(content) }
  File.chmod(0o600, path)
end

def state_id
  return 0 unless File.file?(STATE)
  Integer(read_json(STATE).fetch('last_update_id'))
end

def last_message_id
  return 0 unless File.file?(LAST_UPDATE_ID)
  Integer(File.read(LAST_UPDATE_ID).strip)
end

def write_last_message_id(id)
  temp = LAST_UPDATE_ID + '.tmp'
  private_write(temp, "#{id}\n")
  File.rename(temp, LAST_UPDATE_ID)
end

def write_state(id)
  temp = STATE + '.tmp'
  private_write(temp, JSON.generate('last_update_id' => id) + "\n")
  File.rename(temp, STATE)
end

def compact(value)
  value.to_s.gsub(/[\r\n\t]+/, ' ').gsub(/\s+/, ' ').strip
end

def display_name(data)
  return 'неизвестно' unless data.is_a?(Hash)
  value = compact(data['title'] || data['username'] || [data['first_name'], data['last_name']].compact.join(' '))
  value.empty? ? 'неизвестно' : value
end

def backend_request(method, path, body = nil)
  env_file = File.join(ROOT, '.env')
  values = {}
  if File.file?(env_file)
    File.foreach(env_file) do |line|
      match = line.match(/\A(TELEGRAM_NOTES_BACKEND_URL|TELEGRAM_NOTES_API_KEY)=(.*)\r?\n?\z/)
      values[match[1]] = match[2] if match
    end
  end
  base = ENV['TELEGRAM_NOTES_BACKEND_URL'] || values['TELEGRAM_NOTES_BACKEND_URL']
  key = ENV['TELEGRAM_NOTES_API_KEY'] || values['TELEGRAM_NOTES_API_KEY']
  raise NotesError, 'Задайте TELEGRAM_NOTES_BACKEND_URL и TELEGRAM_NOTES_API_KEY' if base.to_s.empty? || key.to_s.empty?
  uri = URI.join(base.end_with?('/') ? base : base + '/', path)
  request = body ? Net::HTTP::Post.new(uri) : Net::HTTP::Get.new(uri)
  request['Authorization'] = "Bearer #{key}"
  request['Content-Type'] = 'application/json'
  request.body = JSON.generate(body) if body
  response = Net::HTTP.start(uri.host, uri.port, use_ssl: true, open_timeout: 10, read_timeout: 20) do |http|
    http.request(request)
  end
  raise NotesError, "Бэкенд вернул HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)
  JSON.parse(response.body)
rescue JSON::ParserError, KeyError
  raise NotesError, 'Некорректный ответ бэкенда'
end

def transcribe_voice(voice_url)
  uri = URI(voice_url)
  raise NotesError, 'Некорректная ссылка на голосовой файл' unless uri.scheme == 'https'
  response = Net::HTTP.start(uri.host, uri.port, use_ssl: true, open_timeout: 10, read_timeout: 60) do |http|
    http.get(uri.request_uri)
  end
  raise NotesError, "Не удалось скачать голосовое сообщение: HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)
  raise NotesError, 'Локальный Whisper не установлен' unless File.executable?(PYTHON)

  Tempfile.create(['telegram-voice-', '.ogg']) do |audio|
    audio.binmode
    audio.write(response.body)
    audio.flush
    output, _error, status = Open3.capture3(PYTHON, TRANSCRIBE, audio.path, MODEL_DIR)
    raise NotesError, 'Не удалось расшифровать голосовое сообщение локальным Whisper' unless status.success?
    text = JSON.parse(output).fetch('text')
    raise NotesError, 'Некорректная расшифровка Whisper' unless text.is_a?(String)
    text.strip
  end
rescue JSON::ParserError, KeyError
  raise NotesError, 'Некорректный ответ локального Whisper'
end

def fetch
  FileUtils.mkdir_p(NOTES)
  if File.file?(PENDING)
    pending = read_json(PENDING)
    if Integer(pending.fetch('max_update_id')) > state_id
      puts "Ожидает разбора: #{pending.fetch('messages').length} сообщений; файл telegram-notes/.pending.json"
      return
    end
    File.delete(PENDING)
  end

  begin
    batch = backend_request(:get, '/api/updates?limit=100')
    updates = batch.fetch('items')
    raise NotesError, 'Некорректный пакет бэкенда' unless updates.is_a?(Array)
    # The local marker is a second guard against replay if the server cursor
    # was reset or a batch contains updates already committed on this machine.
    marker = [state_id, last_message_id].max
    updates = updates.select { |update| Integer(update.fetch('update_id')) > marker }
  rescue SocketError, IOError, SystemCallError, Timeout::Error, OpenSSL::SSL::SSLError
    abort 'Не удалось получить обновления бэкенда'
  rescue NotesError => error
    abort error.message
  rescue StandardError
    abort 'Не удалось получить обновления бэкенда'
  end

  if updates.empty?
    puts 'Новых обновлений нет'
    return
  end
  begin
    messages = updates.map do |update|
      if update['kind'] == 'text' && update['text'].is_a?(String) && !update['text'].strip.empty?
        kind = 'text'
        content = update['text']
      elsif update['kind'] == 'voice' && update['voice_url'].is_a?(String)
        kind = 'voice'
        content = transcribe_voice(update['voice_url'])
      else
        next
      end
      {
        'update_id' => Integer(update.fetch('update_id')),
        'message_id' => update['message_id'],
        'date_utc' => update['date_utc'],
        'chat' => compact(update['chat'] || 'неизвестно'),
        'sender' => compact(update['sender'] || 'неизвестно'),
        'kind' => kind,
        'text' => content
      }
    end.compact
  rescue SocketError, IOError, SystemCallError, Timeout::Error, OpenSSL::SSL::SSLError
    abort 'Не удалось получить или расшифровать голосовое сообщение'
  rescue NotesError => error
    abort error.message
  rescue StandardError
    abort 'Не удалось обработать пакет Telegram'
  end
  if messages.empty?
    backend_request(:post, '/api/ack', 'batch_cursor' => Integer(batch.fetch('batch_cursor')))
    processed = [Integer(batch['last_update_id'] || state_id), state_id].max
    write_last_message_id([last_message_id, processed].max)
    write_state(processed)
    puts "Текстовых и голосовых сообщений нет; пропущено обновлений: #{updates.length}"
  else
    private_write(PENDING, JSON.pretty_generate('max_update_id' => [Integer(batch['last_update_id'] || state_id), state_id].max, 'batch_cursor' => Integer(batch.fetch('batch_cursor')), 'messages' => messages) + "\n")
    kinds = messages.group_by { |message| message['kind'] }
    puts "Получено сообщений: текстовых #{kinds.fetch('text', []).length}, голосовых #{kinds.fetch('voice', []).length}; файл telegram-notes/.pending.json"
  end
end

def append_notes(path, heading, records)
  unless File.file?(path)
    private_write(path, "# #{heading}\n\n")
  end
  existing = File.read(path)
  additions = records.reject { |record| existing.include?(record.fetch(:marker)) }
  return 0 if additions.empty?
  File.open(path, File::WRONLY | File::CREAT | File::APPEND, 0o600) do |file|
    additions.each { |record| file.write(record.fetch(:line)) }
  end
  File.chmod(0o600, path)
  additions.length
end

def commit
  abort 'Нет ожидающего пакета telegram-notes/.pending.json' unless File.file?(PENDING)
  abort 'Нет классификации telegram-notes/.classified.json' unless File.file?(CLASSIFIED)
  pending = read_json(PENDING)
  classified = read_json(CLASSIFIED)
  messages = pending.fetch('messages')
  items = classified.fetch('items')
  abort 'items должен быть массивом' unless items.is_a?(Array)
  expected = messages.map { |m| Integer(m.fetch('update_id')) }.sort
  actual = items.map { |item| Integer(item.fetch('update_id')) }.sort
  abort 'Не каждый update_id классифицирован ровно один раз' unless expected == actual
  by_id = items.to_h { |item| [Integer(item.fetch('update_id')), item] }
  ideas = []
  tasks = []
  skipped = 0

  messages.each do |message|
    id = Integer(message.fetch('update_id'))
    item = by_id.fetch(id)
    idea_texts = item.fetch('ideas')
    task_texts = item.fetch('tasks')
    abort "Неверные массивы у update_id #{id}" unless idea_texts.is_a?(Array) && task_texts.is_a?(Array)
    abort "Пустая запись без skip у update_id #{id}" if idea_texts.empty? && task_texts.empty? && item['skip'] != true
    abort "skip с заметками у update_id #{id}" if item['skip'] == true && (!idea_texts.empty? || !task_texts.empty?)
    skipped += 1 if item['skip'] == true
    meta = [message['date_utc'] || 'дата не указана', message['chat'], message['sender']].map { |v| compact(v) }.join(' · ')
    { 'idea' => [idea_texts, ideas], 'task' => [task_texts, tasks] }.each do |kind, (texts, target)|
      texts.each_with_index do |note, index|
        abort "Пустая заметка у update_id #{id}" unless note.is_a?(String) && !compact(note).empty?
        marker = "<!-- tg:#{id}:#{kind}:#{index} -->"
        target << { marker: marker, line: "- **#{meta}** — #{compact(note)} #{marker}\n" }
      end
    end
  end

  idea_count = append_notes(File.join(NOTES, 'ideas.md'), 'Идеи из Telegram', ideas)
  task_count = append_notes(File.join(NOTES, 'tasks.md'), 'Задачи из Telegram', tasks)
  backend_request(:post, '/api/ack', 'batch_cursor' => Integer(pending.fetch('batch_cursor')))
  write_last_message_id([last_message_id, expected.last].max)
  write_state(Integer(pending.fetch('max_update_id')))
  File.delete(PENDING)
  File.delete(CLASSIFIED)
  puts "Сохранено: идей #{idea_count}, задач #{task_count}; пропущено сообщений #{skipped}"
end

if $PROGRAM_NAME == __FILE__
  case ARGV.shift
  when 'fetch' then fetch
  when 'commit' then commit
  else abort 'Использование: ruby telegram_notes.rb fetch|commit'
  end
end
