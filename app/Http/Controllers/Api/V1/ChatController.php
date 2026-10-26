<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Models\ChatMessage;
use OGame\Models\User;
use OGame\Services\ChatService;
use OGame\Services\PlayerService;

/**
 * The game's chat: private conversations between two players and the chat of an alliance.
 * A thin layer over ChatService, the same code as the web chat. (These are chat messages, not
 * the report-like game messages of /messages.)
 */
class ChatController extends ApiController
{
    /**
     * Without query: my recent private conversations and the number of unread chat messages.
     * With "with" (a user id): that private conversation. With "alliance=1": my alliance's chat.
     * For a conversation or the alliance chat: limit (1-50, default 50), before_id (older than that
     * message), since_id (only newer than that message), mark_read (0/1, private conversations only).
     * Messages are returned oldest first.
     *
     * @throws ApiException
     */
    public function index(Request $request, PlayerService $player, ChatService $chat): JsonResponse
    {
        $input = $this->validated($request->query(), [
            'with' => ['nullable', 'integer', 'min:1'],
            'alliance' => ['nullable', 'boolean'],
            'limit' => ['nullable', 'integer', 'min:1', 'max:50'],
            'before_id' => ['nullable', 'integer', 'min:1'],
            'since_id' => ['nullable', 'integer', 'min:1'],
            'mark_read' => ['nullable', 'boolean'],
        ]);
        $userId = $player->getId();
        $limit = (int)($input['limit'] ?? 50);
        $beforeId = isset($input['before_id']) ? (int)$input['before_id'] : null;
        $sinceId = isset($input['since_id']) ? (int)$input['since_id'] : 0;

        if (!empty($input['alliance'])) {
            $allianceId = $player->getUser()->alliance_id;
            if ($allianceId === null) {
                throw new ApiException('not_in_alliance', 'The player is not in an alliance.', 422);
            }
            $rows = $chat->getAllianceMessages($allianceId, $limit, $beforeId);

            return $this->ok(['alliance_id' => $allianceId, 'messages' => $this->messagesArray($rows, $sinceId)]);
        }

        if (isset($input['with'])) {
            $partnerId = (int)$input['with'];
            if (User::find($partnerId) === null) {
                throw new ApiException('player_not_found', 'Player not found.', 404);
            }
            $rows = $chat->getConversation($userId, $partnerId, $limit, $beforeId);
            if (!empty($input['mark_read'])) {
                $chat->markAsRead($userId, $partnerId);
            }

            return $this->ok(['with' => $partnerId, 'messages' => $this->messagesArray($rows, $sinceId)]);
        }

        $conversations = [];
        foreach ($chat->getRecentConversations($userId) as $conversation) {
            $conversations[] = [
                'user_id' => $conversation['partner_id'],
                'username' => $conversation['partner_name'],
                'last_message' => $conversation['last_message'],
                'last_message_at' => $conversation['last_message_date']->timestamp,
                'unread' => $conversation['unread_count'],
            ];
        }

        return $this->ok([
            'unread_total' => $chat->getTotalUnreadMessageCount($userId),
            'conversations' => $conversations,
        ]);
    }

    /**
     * Send a chat message. Body: text (up to 2000 characters) and either user_id (private message) or
     * alliance=true (to my alliance's chat); reply_to (id of a chat message, optional).
     *
     * @throws ApiException
     */
    public function send(Request $request, PlayerService $player, ChatService $chat): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'text' => ['required', 'string', 'min:1', 'max:2000'],
            'user_id' => ['nullable', 'integer', 'min:1'],
            'alliance' => ['nullable', 'boolean'],
            'reply_to' => ['nullable', 'integer', 'min:1'],
        ]);
        $this->assertNotInVacationMode($player);
        $replyTo = isset($input['reply_to']) ? (int)$input['reply_to'] : null;

        if (!empty($input['alliance'])) {
            $allianceId = $player->getUser()->alliance_id;
            if ($allianceId === null) {
                throw new ApiException('not_in_alliance', 'The player is not in an alliance.', 422);
            }
            $message = $chat->sendAllianceMessage($player->getId(), $allianceId, $input['text'], $replyTo);

            return $this->ok(['message' => $this->messageArray($message)], 201);
        }

        if (!isset($input['user_id'])) {
            throw new ApiException('validation_failed', 'Give user_id (private message) or alliance=true.', 422, ['user_id' => ['Required unless alliance is true.']]);
        }

        return $this->sendDirect($player, $chat, (int)$input['user_id'], $input['text'], $replyTo);
    }

    /**
     * Send a private message to another player. Body: user_id, text (up to 2000 characters).
     * The recipient reads it in the chat (GET /chat?with=...).
     *
     * @throws ApiException
     */
    public function sendMessage(Request $request, PlayerService $player, ChatService $chat): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'user_id' => ['required', 'integer', 'min:1'],
            'text' => ['required', 'string', 'min:1', 'max:2000'],
        ]);
        $this->assertNotInVacationMode($player);

        return $this->sendDirect($player, $chat, (int)$input['user_id'], $input['text'], null);
    }

    /**
     * @throws ApiException
     */
    private function sendDirect(PlayerService $player, ChatService $chat, int $recipientId, string $text, int|null $replyTo): JsonResponse
    {
        if ($recipientId === $player->getId()) {
            throw new ApiException('same_user', 'You cannot message yourself.', 422);
        }
        if (User::find($recipientId) === null) {
            throw new ApiException('player_not_found', 'Player not found.', 404);
        }
        if (!$chat->canMessagePlayer($player->getId(), $recipientId)) {
            throw new ApiException('ignored_by_recipient', 'The recipient ignores you.', 403);
        }

        $message = $chat->sendDirectMessage($player->getId(), $recipientId, $text, $replyTo);

        return $this->ok(['message' => $this->messageArray($message)], 201);
    }

    /**
     * @param iterable<ChatMessage> $rows
     * @return list<array<string, mixed>>
     */
    private function messagesArray(iterable $rows, int $sinceId): array
    {
        $messages = [];
        foreach ($rows as $row) {
            if ($row->id > $sinceId) {
                $messages[] = $this->messageArray($row);
            }
        }

        return $messages;
    }

    /**
     * @return array<string, mixed>
     */
    private function messageArray(ChatMessage $message): array
    {
        return [
            'id' => $message->id,
            'from_user_id' => $message->sender_id,
            'from' => $message->sender->username ?? '',
            'to_user_id' => $message->recipient_id,
            'alliance_id' => $message->alliance_id,
            'text' => $message->message,
            'reply_to' => $message->reply_to_id,
            'created_at' => $message->created_at?->timestamp,
        ];
    }
}
