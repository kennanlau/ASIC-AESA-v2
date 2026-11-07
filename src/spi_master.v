`default_nettype none
// SPI master v2
//  * Mode 0 by default (CPOL=0, CPHA=0): MOSI is valid before each SCLK rising
//    edge and changes on falling edges. The ADAR1000 samples SDIO on the
//    rising edge of SCLK in write mode (datasheet pin description).
//  * cpha=1 gives the v1 timing (data changes on rising edges).
//  * miso is sampled on the edge opposite to the one that changes MOSI.
module spi_master (
    input  wire        clk,
    input  wire        rst,
    input  wire        start,     // 1-cycle pulse, accepted in IDLE
    input  wire        cpha,
    input  wire [5:0]  half_div,  // SCLK half period in clk cycles (min 2)
    input  wire [23:0] tx,
    input  wire        miso,
    output reg         sclk,
    output reg         mosi,
    output reg         cs_n,
    output reg         busy,
    output reg         done,      // 1-cycle pulse at the end of a frame
    output reg  [7:0]  rx_byte    // last 8 bits shifted in (read data)
);
    localparam IDLE = 3'd0, LOW = 3'd1, HIGH = 3'd2, TRAIL = 3'd3, GAP = 3'd4;
    reg [2:0]  state;
    reg [23:0] sh;
    reg [4:0]  bits_left;
    reg [5:0]  tmr;
    reg [7:0]  rx_sh;
    wire [5:0] hd   = (half_div < 6'd2) ? 6'd2 : half_div;
    wire       tick = (tmr == hd - 6'd1);

    always @(posedge clk) begin
        if (rst) begin
            state <= IDLE; sclk <= 1'b0; mosi <= 1'b0; cs_n <= 1'b1;
            busy <= 1'b0; done <= 1'b0; tmr <= 6'd0; bits_left <= 5'd0;
            sh <= 24'd0; rx_sh <= 8'd0; rx_byte <= 8'd0;
        end else begin
            done <= 1'b0;
            case (state)
                IDLE: begin
                    sclk <= 1'b0;
                    if (start) begin
                        sh <= tx; bits_left <= 5'd24; tmr <= 6'd0;
                        cs_n <= 1'b0; busy <= 1'b1;
                        if (!cpha) mosi <= tx[23];       // first bit before first rising edge
                        state <= LOW;
                    end
                end
                LOW: if (tick) begin                     // SCLK rising edge
                    tmr <= 6'd0; sclk <= 1'b1;
                    if (cpha) mosi <= sh[23];
                    else      rx_sh <= {rx_sh[6:0], miso};
                    state <= HIGH;
                end else tmr <= tmr + 6'd1;
                HIGH: if (tick) begin                    // SCLK falling edge
                    tmr <= 6'd0; sclk <= 1'b0;
                    if (cpha) rx_sh <= {rx_sh[6:0], miso};
                    sh <= {sh[22:0], 1'b0};
                    bits_left <= bits_left - 5'd1;
                    if (bits_left == 5'd1) state <= TRAIL;
                    else begin
                        if (!cpha) mosi <= sh[22];       // next bit, half a period before its rising edge
                        state <= LOW;
                    end
                end else tmr <= tmr + 6'd1;
                TRAIL: if (tick) begin                   // CS hold after the last edge
                    tmr <= 6'd0; cs_n <= 1'b1; mosi <= 1'b0; rx_byte <= rx_sh; state <= GAP;
                end else tmr <= tmr + 6'd1;
                GAP: if (tick) begin                     // minimum CS high time
                    tmr <= 6'd0; busy <= 1'b0; done <= 1'b1; state <= IDLE;
                end else tmr <= tmr + 6'd1;
                default: state <= IDLE;
            endcase
        end
    end
endmodule
