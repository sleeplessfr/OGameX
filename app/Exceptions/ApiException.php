<?php

namespace OGame\Exceptions;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use RuntimeException;

/**
 * An error that is reported to API clients as a machine readable JSON error.
 *
 * Laravel calls render() automatically when this exception is thrown anywhere
 * during an API request (controllers and middleware included).
 */
class ApiException extends RuntimeException
{
    /**
     * @param string $errorCode Stable, machine readable code (e.g. "not_enough_resources").
     * @param string $message Human readable explanation.
     * @param int $status HTTP status code.
     * @param array<string, mixed> $details Optional extra data for the client.
     */
    public function __construct(
        public readonly string $errorCode,
        string $message,
        public readonly int $status = 422,
        public readonly array $details = [],
    ) {
        parent::__construct($message);
    }

    /**
     * Render the exception as the standard API error envelope.
     */
    public function render(Request $request): JsonResponse
    {
        $error = [
            'code' => $this->errorCode,
            'message' => $this->getMessage(),
        ];
        if ($this->details !== []) {
            $error['details'] = $this->details;
        }

        return response()->json(['success' => false, 'error' => $error], $this->status);
    }
}
